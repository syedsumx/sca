package s3

import (
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
)

// Headers stored with an object and returned on GET/HEAD.
var storedHeaders = []string{"Content-Encoding", "Content-Disposition", "Content-Language", "Cache-Control", "Expires"}

func objectHeaders(r *http.Request) (string, map[string]string, map[string]string) {
	ct := r.Header.Get("Content-Type")
	if ct == "" {
		ct = "binary/octet-stream"
	}
	hs := map[string]string{}
	for _, h := range storedHeaders {
		v := r.Header.Get(h)
		if h == "Content-Encoding" {
			var keep []string
			for _, t := range strings.Split(v, ",") {
				if t = strings.TrimSpace(t); t != "" && !strings.EqualFold(t, "aws-chunked") {
					keep = append(keep, t)
				}
			}
			v = strings.Join(keep, ",")
		}
		if v != "" {
			hs[h] = v
		}
	}
	um := map[string]string{}
	for k, v := range r.Header {
		lk := strings.ToLower(k)
		if strings.HasPrefix(lk, "x-amz-meta-") && len(v) > 0 {
			um[strings.TrimPrefix(lk, "x-amz-meta-")] = strings.Join(v, ",")
		}
	}
	if len(hs) == 0 {
		hs = nil
	}
	if len(um) == 0 {
		um = nil
	}
	return ct, hs, um
}

func metaTooLarge(um map[string]string) bool {
	n := 0
	for k, v := range um {
		n += len(k) + len(v)
	}
	return n > 2048
}

// receive streams a request body into a sealed blob, verifying signatures, SHA-256, Content-MD5 and
// checksums. compute lists checksum algorithms to compute even if the client sent none.
func (s *Server) receive(c *reqCtx, limit int64, compute ...string) (engine.Segment, []byte, map[string]string, *apiError) {
	r := c.r
	if r.ContentLength < 0 && r.Header.Get("X-Amz-Decoded-Content-Length") == "" {
		return engine.Segment{}, nil, nil, errMissingLength
	}
	if r.ContentLength > limit && r.Header.Get("X-Amz-Decoded-Content-Length") == "" {
		return engine.Segment{}, nil, nil, errEntityTooLarge
	}
	if dl := r.Header.Get("X-Amz-Decoded-Content-Length"); dl != "" {
		if n, err := strconv.ParseInt(dl, 10, 64); err == nil && n > limit {
			return engine.Segment{}, nil, nil, errEntityTooLarge
		}
	}
	b, err := newBody(r, c.auth)
	if err != nil {
		return engine.Segment{}, nil, nil, mapErr(err)
	}
	for _, a := range compute {
		if validAlgo(a) && b.sums[a] == nil {
			b.sums[a] = newChecksum(a)
		}
	}
	blob, err := s.eng.NewBlob()
	if err != nil {
		return engine.Segment{}, nil, nil, errInternal
	}
	n, err := io.Copy(blob, io.LimitReader(b, limit+1))
	if err == nil {
		// io.Copy stops at the limit without seeing EOF; make the body verify itself.
		if n <= limit {
			if _, err2 := b.Read(make([]byte, 1)); err2 != nil && !errors.Is(err2, io.EOF) {
				err = err2
			}
		}
	}
	if err != nil {
		blob.Abort()
		return engine.Segment{}, nil, nil, mapErr(err)
	}
	if n > limit {
		blob.Abort()
		return engine.Segment{}, nil, nil, errEntityTooLarge
	}
	sum := blob.MD5()
	if md := r.Header.Get("Content-Md5"); md != "" && base64.StdEncoding.EncodeToString(sum) != md {
		blob.Abort()
		return engine.Segment{}, nil, nil, errBadDigestErr
	}
	seg, err := blob.Seal()
	if err != nil {
		s.log.Error("seal blob", "err", err)
		return engine.Segment{}, nil, nil, errInternal
	}
	return seg, sum, b.Checksums(), nil
}

func writeChecksumHeaders(h http.Header, cs map[string]string, ctype string) {
	for a, v := range cs {
		h.Set("x-amz-checksum-"+a, v)
	}
	if len(cs) > 0 && ctype != "" {
		h.Set("x-amz-checksum-type", ctype)
	}
}

func (s *Server) putObject(c *reqCtx) {
	r := c.r
	ct, hs, um := objectHeaders(r)
	if metaTooLarge(um) {
		s.fail(c, newErr("MetadataTooLarge", "Your metadata headers exceed the maximum allowed metadata size.", http.StatusBadRequest))
		return
	}
	if _, err := s.eng.HeadBucket(c.bucket); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	var compute []string
	if a := strings.ToLower(r.Header.Get("X-Amz-Checksum-Algorithm")); a != "" {
		compute = append(compute, a)
	}
	seg, sum, cs, aerr := s.receive(c, s.maxObj, compute...)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	obj := engine.Object{Key: c.key, Size: seg.Size, ETag: hex.EncodeToString(sum), Modified: s.now().UTC(),
		ContentType: ct, Headers: hs, UserMeta: um, Checksums: cs, ChecksumType: "FULL_OBJECT",
		Segments: []engine.Segment{seg}}
	cond := engine.Condition{IfMatch: r.Header.Get("If-Match"), IfNoneMatch: r.Header.Get("If-None-Match")}
	if err := s.eng.PutObject(c.bucket, obj, cond); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	c.w.Header().Set("ETag", `"`+obj.ETag+`"`)
	writeChecksumHeaders(c.w.Header(), cs, "FULL_OBJECT")
	c.w.WriteHeader(http.StatusOK)
}

type notModified struct{}

func (notModified) Error() string { return "not modified" }

// parseRange handles a single "bytes=" range. ok=false means the header is ignored (serve it all).
func parseRange(h string, size int64) (off, n int64, ok bool, aerr *apiError) {
	if !strings.HasPrefix(h, "bytes=") || strings.Contains(h, ",") {
		return 0, size, false, nil
	}
	spec := strings.TrimSpace(strings.TrimPrefix(h, "bytes="))
	a, b, found := strings.Cut(spec, "-")
	if !found {
		return 0, size, false, nil
	}
	if a == "" {
		k, err := strconv.ParseInt(b, 10, 64)
		if err != nil || k < 0 {
			return 0, size, false, nil
		}
		if k == 0 || size == 0 {
			return 0, 0, false, errInvalidRange
		}
		if k > size {
			k = size
		}
		return size - k, k, true, nil
	}
	start, err := strconv.ParseInt(a, 10, 64)
	if err != nil || start < 0 {
		return 0, size, false, nil
	}
	end := size - 1
	if b != "" {
		e, err := strconv.ParseInt(b, 10, 64)
		if err != nil || e < start {
			return 0, size, false, nil
		}
		if e < end {
			end = e
		}
	}
	if start >= size {
		return 0, 0, false, errInvalidRange
	}
	return start, end - start + 1, true, nil
}

func checkReadConditions(r *http.Request, o engine.Object) error {
	etag := `"` + o.ETag + `"`
	mod := o.Modified.Truncate(time.Second)
	if v := r.Header.Get("If-Match"); v != "" {
		if !etagMatchesHeader(v, etag) {
			return errPrecondition
		}
	} else if v := r.Header.Get("If-Unmodified-Since"); v != "" {
		if t, err := http.ParseTime(v); err == nil && mod.After(t) {
			return errPrecondition
		}
	}
	if v := r.Header.Get("If-None-Match"); v != "" {
		if etagMatchesHeader(v, etag) {
			return notModified{}
		}
	} else if v := r.Header.Get("If-Modified-Since"); v != "" {
		if t, err := http.ParseTime(v); err == nil && !mod.After(t) {
			return notModified{}
		}
	}
	return nil
}

func etagMatchesHeader(header, etag string) bool {
	for _, t := range strings.Split(header, ",") {
		t = strings.TrimPrefix(strings.TrimSpace(t), "W/")
		if t == "*" || strings.Trim(t, `"`) == strings.Trim(etag, `"`) {
			return true
		}
	}
	return false
}

func (s *Server) getObject(c *reqCtx, q url.Values, head bool) {
	r := c.r
	partNumber := 0
	if v := q.Get("partNumber"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > engine.MaxParts {
			s.fail(c, errInvalidArgument)
			return
		}
		partNumber = n
	}
	var status = http.StatusOK
	var off, length int64
	ranged := false
	obj, rc, err := s.eng.GetFresh(c.bucket, c.key, func(o engine.Object) (int64, int64, error) {
		if err := checkReadConditions(r, o); err != nil {
			return 0, 0, err
		}
		off, length, ranged, status = 0, o.Size, false, http.StatusOK
		if partNumber > 0 {
			if partNumber > len(o.Segments) {
				return 0, 0, newErr("InvalidPartNumber", "The requested partnumber is not satisfiable", http.StatusRequestedRangeNotSatisfiable)
			}
			for i := 0; i < partNumber-1; i++ {
				off += o.Segments[i].Size
			}
			length = o.Segments[partNumber-1].Size
			ranged = o.PartCount > 0
			if ranged {
				status = http.StatusPartialContent
			}
			return off, length, nil
		}
		if h := r.Header.Get("Range"); h != "" {
			o2, n2, ok, aerr := parseRange(h, o.Size)
			if aerr != nil {
				return 0, 0, aerr
			}
			if ok {
				off, length, ranged, status = o2, n2, true, http.StatusPartialContent
			}
		}
		if head {
			return off, 0, nil
		}
		return off, length, nil
	})
	if err != nil {
		var nm notModified
		if errors.As(err, &nm) {
			h := c.w.Header()
			h.Set("ETag", `"`+obj.ETag+`"`)
			h.Set("Last-Modified", obj.Modified.UTC().Format(http.TimeFormat))
			c.w.WriteHeader(http.StatusNotModified)
			return
		}
		if errors.Is(err, errInvalidRange) {
			c.w.Header().Set("Content-Range", fmt.Sprintf("bytes */%d", obj.Size))
		}
		s.fail(c, mapErr(err))
		return
	}
	defer rc.Close()
	h := c.w.Header()
	h.Set("Content-Type", obj.ContentType)
	for k, v := range obj.Headers {
		h.Set(k, v)
	}
	for k, v := range obj.UserMeta {
		h["x-amz-meta-"+k] = []string{v} // as stored: clients read metadata names case-sensitively
	}
	for _, o := range []string{"content-type", "content-language", "expires", "cache-control", "content-disposition", "content-encoding"} {
		if v := q.Get("response-" + o); v != "" {
			h.Set(o, v)
		}
	}
	h.Set("ETag", `"`+obj.ETag+`"`)
	h.Set("Last-Modified", obj.Modified.UTC().Format(http.TimeFormat))
	h.Set("Accept-Ranges", "bytes")
	if partNumber > 0 && obj.PartCount > 0 {
		h.Set("x-amz-mp-parts-count", strconv.Itoa(obj.PartCount))
	}
	if !ranged && strings.EqualFold(r.Header.Get("X-Amz-Checksum-Mode"), "ENABLED") {
		writeChecksumHeaders(h, obj.Checksums, obj.ChecksumType)
	}
	h.Set("Content-Length", strconv.FormatInt(length, 10))
	if ranged {
		h.Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", off, off+length-1, obj.Size))
	}
	c.w.WriteHeader(status)
	if head {
		return
	}
	if _, err := io.Copy(c.w, rc); err != nil {
		s.log.Warn("send object", "bucket", c.bucket, "key", c.key, "err", err)
	}
}

// copySource parses x-amz-copy-source ("/bucket/key", URL-encoded, optional ?versionId).
func copySource(v string) (string, string, *apiError) {
	if i := strings.Index(v, "?"); i >= 0 {
		v = v[:i]
	}
	dec, err := url.PathUnescape(v)
	if err != nil {
		return "", "", errInvalidCopySource
	}
	dec = strings.TrimPrefix(dec, "/")
	b, k, ok := strings.Cut(dec, "/")
	if !ok || b == "" || k == "" {
		return "", "", errInvalidCopySource
	}
	return b, k, nil
}

func checkCopyConditions(r *http.Request, o engine.Object) *apiError {
	etag := `"` + o.ETag + `"`
	mod := o.Modified.Truncate(time.Second)
	if v := r.Header.Get("X-Amz-Copy-Source-If-Match"); v != "" && !etagMatchesHeader(v, etag) {
		return errPrecondition
	}
	if v := r.Header.Get("X-Amz-Copy-Source-If-None-Match"); v != "" && etagMatchesHeader(v, etag) {
		return errPrecondition
	}
	if v := r.Header.Get("X-Amz-Copy-Source-If-Unmodified-Since"); v != "" {
		if t, err := http.ParseTime(v); err == nil && mod.After(t) {
			return errPrecondition
		}
	}
	if v := r.Header.Get("X-Amz-Copy-Source-If-Modified-Since"); v != "" {
		if t, err := http.ParseTime(v); err == nil && !mod.After(t) {
			return errPrecondition
		}
	}
	return nil
}

// copyBytes writes [off, off+n) of a source object into a new sealed blob.
func (s *Server) copyBytes(c *reqCtx, srcBucket, srcKey string, rng string, algos []string) (engine.Object, engine.Segment,
	[]byte, map[string]string, *apiError) {
	var off, n int64
	src, rc, err := s.eng.GetFresh(srcBucket, srcKey, func(o engine.Object) (int64, int64, error) {
		if aerr := checkCopyConditions(c.r, o); aerr != nil {
			return 0, 0, aerr
		}
		off, n = 0, o.Size
		if rng != "" {
			a, b, ok := strings.Cut(strings.TrimPrefix(rng, "bytes="), "-")
			start, e1 := strconv.ParseInt(a, 10, 64)
			end, e2 := strconv.ParseInt(b, 10, 64)
			if !ok || e1 != nil || e2 != nil || start < 0 || end < start || end >= o.Size {
				return 0, 0, errInvalidRange
			}
			off, n = start, end-start+1
		}
		return off, n, nil
	})
	if err != nil {
		return src, engine.Segment{}, nil, nil, mapErr(err)
	}
	defer rc.Close()
	var hashes []io.Writer
	sums := map[string]hash.Hash{}
	for _, a := range algos {
		if validAlgo(a) {
			h := newChecksum(a)
			sums[a] = h
			hashes = append(hashes, h)
		}
	}
	blob, err := s.eng.NewBlob(hashes...)
	if err != nil {
		return src, engine.Segment{}, nil, nil, errInternal
	}
	if _, err := io.Copy(blob, rc); err != nil {
		blob.Abort()
		return src, engine.Segment{}, nil, nil, errInternal
	}
	sum := blob.MD5()
	seg, err := blob.Seal()
	if err != nil {
		return src, engine.Segment{}, nil, nil, errInternal
	}
	cs := map[string]string{}
	for a, h := range sums {
		cs[a] = base64.StdEncoding.EncodeToString(h.Sum(nil))
	}
	if len(cs) == 0 {
		cs = nil
	}
	return src, seg, sum, cs, nil
}

func (s *Server) copyObject(c *reqCtx) {
	r := c.r
	srcBucket, srcKey, aerr := copySource(r.Header.Get("X-Amz-Copy-Source"))
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	if !c.auth.cred.allows(srcBucket) {
		s.fail(c, errAccessDenied)
		return
	}
	directive := strings.ToUpper(r.Header.Get("X-Amz-Metadata-Directive"))
	if directive == "" {
		directive = "COPY"
	}
	if directive != "COPY" && directive != "REPLACE" {
		s.fail(c, errInvalidArgument)
		return
	}
	newAlgo := strings.ToLower(r.Header.Get("X-Amz-Checksum-Algorithm"))
	if srcBucket == c.bucket && srcKey == c.key && directive == "COPY" && newAlgo == "" {
		s.fail(c, errCopyToItself)
		return
	}
	if _, err := s.eng.HeadBucket(c.bucket); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	meta, err := s.eng.GetObject(srcBucket, srcKey)
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	var algos []string
	if newAlgo != "" {
		algos = []string{newAlgo}
	} else {
		for a, v := range meta.Checksums {
			if !strings.Contains(v, "-") {
				algos = append(algos, a)
			}
		}
	}
	src, seg, sum, cs, aerr := s.copyBytes(c, srcBucket, srcKey, "", algos)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	obj := engine.Object{Key: c.key, Size: seg.Size, ETag: hex.EncodeToString(sum), Modified: s.now().UTC(),
		ContentType: src.ContentType, Headers: src.Headers, UserMeta: src.UserMeta, Checksums: cs,
		ChecksumType: "FULL_OBJECT", Segments: []engine.Segment{seg}}
	if directive == "REPLACE" {
		obj.ContentType, obj.Headers, obj.UserMeta = objectHeaders(r)
	}
	cond := engine.Condition{IfMatch: r.Header.Get("If-Match"), IfNoneMatch: r.Header.Get("If-None-Match")}
	if err := s.eng.PutObject(c.bucket, obj, cond); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	writeChecksumHeaders(c.w.Header(), cs, "FULL_OBJECT")
	s.xml(c, http.StatusOK, copyObjectResult{Xmlns: xmlns, LastModified: isoTime(obj.Modified), ETag: `"` + obj.ETag + `"`})
}
