package s3

import (
	"crypto/md5"
	"encoding/base64"
	"encoding/xml"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
)

var bucketNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`)

func validBucketName(n string) bool {
	if !bucketNameRE.MatchString(n) || strings.Contains(n, "..") || strings.Contains(n, ".-") ||
		strings.Contains(n, "-.") {
		return false
	}
	return net.ParseIP(n) == nil
}

func (s *Server) listBuckets(c *reqCtx) {
	bs, err := s.eng.ListBuckets()
	if err != nil {
		s.fail(c, errInternal)
		return
	}
	out := listAllMyBuckets{Xmlns: xmlns, Owner: owner{ID: c.auth.cred.AccessKey, DisplayName: c.auth.cred.AccessKey}}
	for _, b := range bs {
		if c.auth.cred.allows(b.Name) {
			out.Buckets = append(out.Buckets, bucket{Name: b.Name, CreationDate: isoTime(b.Created)})
		}
	}
	s.xml(c, http.StatusOK, out)
}

func (s *Server) createBucket(c *reqCtx) {
	if !c.auth.cred.Admin {
		s.fail(c, errAccessDenied)
		return
	}
	if !validBucketName(c.bucket) {
		s.fail(c, errInvalidBucketName)
		return
	}
	if c.r.ContentLength != 0 {
		b, aerr := s.readSmallBody(c, 64<<10)
		if aerr != nil {
			s.fail(c, aerr)
			return
		}
		if len(strings.TrimSpace(string(b))) > 0 {
			var cfg createBucketConfiguration
			if xml.Unmarshal(b, &cfg) != nil {
				s.fail(c, errMalformedXML)
				return
			}
		}
	}
	if err := s.eng.CreateBucket(c.bucket); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	c.w.Header().Set("Location", "/"+c.bucket)
	c.w.WriteHeader(http.StatusOK)
}

func (s *Server) deleteBucket(c *reqCtx) {
	if !c.auth.cred.Admin {
		s.fail(c, errAccessDenied)
		return
	}
	if err := s.eng.DeleteBucket(c.bucket); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	c.w.WriteHeader(http.StatusNoContent)
}

// readSmallBody reads and verifies a small request body (XML documents).
func (s *Server) readSmallBody(c *reqCtx, limit int64) ([]byte, *apiError) {
	b, err := newBody(c.r, c.auth)
	if err != nil {
		return nil, mapErr(err)
	}
	data, err := io.ReadAll(io.LimitReader(b, limit+1))
	if err != nil {
		return nil, mapErr(err)
	}
	if int64(len(data)) > limit {
		return nil, errEntityTooLarge
	}
	if md := c.r.Header.Get("Content-Md5"); md != "" {
		sum := md5.Sum(data)
		if base64.StdEncoding.EncodeToString(sum[:]) != md {
			return nil, errBadDigestErr
		}
	}
	return data, nil
}

func (s *Server) deleteObjects(c *reqCtx) {
	data, aerr := s.readSmallBody(c, 2<<20)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	var req deleteRequest
	if xml.Unmarshal(data, &req) != nil || len(req.Objects) == 0 || len(req.Objects) > 1000 {
		s.fail(c, errMalformedXML)
		return
	}
	if _, err := s.eng.HeadBucket(c.bucket); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	res := deleteResult{Xmlns: xmlns}
	for _, o := range req.Objects {
		if err := s.eng.DeleteObject(c.bucket, o.Key, engine.Condition{}); err != nil {
			ae := mapErr(err)
			res.Errors = append(res.Errors, deleteErrXML{Key: o.Key, Code: ae.Code, Message: ae.Message})
			continue
		}
		if !req.Quiet {
			res.Deleted = append(res.Deleted, deletedXML{Key: o.Key})
		}
	}
	s.xml(c, http.StatusOK, res)
}

func encodeKey(k, enc string) string {
	if enc != "url" {
		return k
	}
	return awsEncode(k, true) // RFC 3986: "a b" -> "a%20b", "a+b" -> "a%2Bb", slashes kept
}

func maxKeys(q url.Values, name string, def int) (int, *apiError) {
	v := q.Get(name)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return 0, errInvalidArgument
	}
	if n > 1000 {
		n = 1000
	}
	return n, nil
}

func (s *Server) toContents(o engine.Object, enc string, withOwner bool, c *reqCtx) contents {
	ct := contents{Key: encodeKey(o.Key, enc), LastModified: isoTime(o.Modified), ETag: `"` + o.ETag + `"`,
		Size: o.Size, StorageClass: "STANDARD"}
	if withOwner {
		ct.Owner = &owner{ID: c.auth.cred.AccessKey, DisplayName: c.auth.cred.AccessKey}
	}
	for a := range o.Checksums {
		ct.ChecksumAlgo = append(ct.ChecksumAlgo, strings.ToUpper(a))
	}
	if len(ct.ChecksumAlgo) > 0 {
		ct.ChecksumType = o.ChecksumType
	}
	return ct
}

func (s *Server) listV2(c *reqCtx, q url.Values) {
	mk, aerr := maxKeys(q, "max-keys", 1000)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	enc := q.Get("encoding-type")
	if enc != "" && enc != "url" {
		s.fail(c, errInvalidArgument)
		return
	}
	after := q.Get("start-after")
	token := q.Get("continuation-token")
	if token != "" {
		b, err := base64.RawURLEncoding.DecodeString(token)
		if err != nil {
			s.fail(c, newErr("InvalidArgument", "The continuation token provided is incorrect", http.StatusBadRequest))
			return
		}
		after = string(b)
	}
	res, err := s.eng.List(c.bucket, engine.ListOptions{Prefix: q.Get("prefix"), Delimiter: q.Get("delimiter"),
		After: after, MaxKeys: mk})
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	out := listV2{Xmlns: xmlns, Name: c.bucket, Prefix: encodeKey(q.Get("prefix"), enc),
		Delimiter: encodeKey(q.Get("delimiter"), enc), StartAfter: encodeKey(q.Get("start-after"), enc),
		ContinuationToken: token, MaxKeys: mk, EncodingType: enc, IsTruncated: res.Truncated}
	withOwner := q.Get("fetch-owner") == "true"
	for _, o := range res.Objects {
		out.Contents = append(out.Contents, s.toContents(o, enc, withOwner, c))
	}
	for _, p := range res.CommonPrefixes {
		out.CommonPrefixes = append(out.CommonPrefixes, commonPrefix{Prefix: encodeKey(p, enc)})
	}
	out.KeyCount = len(out.Contents) + len(out.CommonPrefixes)
	if res.Truncated {
		out.NextContinuationToken = base64.RawURLEncoding.EncodeToString([]byte(res.NextAfter))
	}
	s.xml(c, http.StatusOK, out)
}

func (s *Server) listV1(c *reqCtx, q url.Values) {
	mk, aerr := maxKeys(q, "max-keys", 1000)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	enc := q.Get("encoding-type")
	if enc != "" && enc != "url" {
		s.fail(c, errInvalidArgument)
		return
	}
	res, err := s.eng.List(c.bucket, engine.ListOptions{Prefix: q.Get("prefix"), Delimiter: q.Get("delimiter"),
		After: q.Get("marker"), MaxKeys: mk})
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	out := listV1{Xmlns: xmlns, Name: c.bucket, Prefix: encodeKey(q.Get("prefix"), enc), Marker: encodeKey(q.Get("marker"), enc),
		Delimiter: encodeKey(q.Get("delimiter"), enc), MaxKeys: mk, EncodingType: enc, IsTruncated: res.Truncated}
	for _, o := range res.Objects {
		out.Contents = append(out.Contents, s.toContents(o, enc, true, c))
	}
	for _, p := range res.CommonPrefixes {
		out.CommonPrefixes = append(out.CommonPrefixes, commonPrefix{Prefix: encodeKey(p, enc)})
	}
	if res.Truncated && q.Get("delimiter") != "" {
		out.NextMarker = encodeKey(res.NextAfter, enc)
	}
	s.xml(c, http.StatusOK, out)
}

func (s *Server) listUploads(c *reqCtx, q url.Values) {
	mu, aerr := maxKeys(q, "max-uploads", 1000)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	ups, err := s.eng.ListUploads(c.bucket, q.Get("prefix"))
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	keyMarker, idMarker := q.Get("key-marker"), q.Get("upload-id-marker")
	out := listUploadsResult{Xmlns: xmlns, Bucket: c.bucket, KeyMarker: keyMarker, UploadIDMarker: idMarker,
		Prefix: q.Get("prefix"), Delimiter: q.Get("delimiter"), MaxUploads: mu}
	o := owner{ID: c.auth.cred.AccessKey, DisplayName: c.auth.cred.AccessKey}
	seenPrefix := map[string]bool{}
	for _, u := range ups {
		if keyMarker != "" && (u.Key < keyMarker || (u.Key == keyMarker && (idMarker == "" || u.ID <= idMarker))) {
			continue
		}
		if d := q.Get("delimiter"); d != "" {
			rest := strings.TrimPrefix(u.Key, q.Get("prefix"))
			if i := strings.Index(rest, d); i >= 0 {
				cp := q.Get("prefix") + rest[:i+len(d)]
				if !seenPrefix[cp] {
					seenPrefix[cp] = true
					out.CommonPrefixes = append(out.CommonPrefixes, commonPrefix{Prefix: cp})
				}
				continue
			}
		}
		if len(out.Uploads) >= mu {
			out.IsTruncated = true
			break
		}
		out.Uploads = append(out.Uploads, uploadXML{Key: u.Key, UploadID: u.ID, Initiator: o, Owner: o,
			StorageClass: "STANDARD", Initiated: isoTime(u.Initiated)})
		out.NextKeyMarker, out.NextUploadIDMarker = u.Key, u.ID
	}
	s.xml(c, http.StatusOK, out)
}
