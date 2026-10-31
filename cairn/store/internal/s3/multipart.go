package s3

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/xml"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
)

func (s *Server) createUpload(c *reqCtx) {
	r := c.r
	ct, hs, um := objectHeaders(r)
	if metaTooLarge(um) {
		s.fail(c, newErr("MetadataTooLarge", "Your metadata headers exceed the maximum allowed metadata size.", http.StatusBadRequest))
		return
	}
	algo := strings.ToLower(r.Header.Get("X-Amz-Checksum-Algorithm"))
	if algo != "" && !validAlgo(algo) {
		s.fail(c, errInvalidArgument)
		return
	}
	ctype := strings.ToUpper(r.Header.Get("X-Amz-Checksum-Type"))
	if algo != "" && ctype == "" {
		ctype = "COMPOSITE"
		if algo == "crc64nvme" {
			ctype = "FULL_OBJECT"
		}
	}
	if ctype != "" && ctype != "COMPOSITE" && ctype != "FULL_OBJECT" {
		s.fail(c, errInvalidArgument)
		return
	}
	if ctype == "FULL_OBJECT" && (algo == "sha1" || algo == "sha256") {
		s.fail(c, newErr("InvalidRequest", "The FULL_OBJECT checksum type is not supported for SHA algorithms.", http.StatusBadRequest))
		return
	}
	u, err := s.eng.CreateUpload(engine.Upload{Bucket: c.bucket, Key: c.key, ContentType: ct, Headers: hs, UserMeta: um,
		ChecksumAlgo: algo, ChecksumType: ctype})
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	if algo != "" {
		c.w.Header().Set("x-amz-checksum-algorithm", strings.ToUpper(algo))
		c.w.Header().Set("x-amz-checksum-type", ctype)
	}
	s.xml(c, http.StatusOK, initiateResult{Xmlns: xmlns, Bucket: c.bucket, Key: c.key, UploadID: u.ID})
}

func partNumberOf(q url.Values) (int, *apiError) {
	n, err := strconv.Atoi(q.Get("partNumber"))
	if err != nil || n < 1 || n > engine.MaxParts {
		return 0, newErr("InvalidArgument", "Part number must be an integer between 1 and 10000, inclusive", http.StatusBadRequest)
	}
	return n, nil
}

func (s *Server) uploadPart(c *reqCtx, q url.Values) {
	n, aerr := partNumberOf(q)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	u, err := s.eng.GetUpload(c.bucket, c.key, q.Get("uploadId"))
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	var compute []string
	if u.ChecksumAlgo != "" {
		compute = append(compute, u.ChecksumAlgo)
	}
	seg, sum, cs, aerr := s.receive(c, 5<<30, compute...)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	p := engine.Part{Number: n, Blob: seg.Blob, Size: seg.Size, ETag: hex.EncodeToString(sum), MD5: sum, Checksum: cs,
		Modified: s.now().UTC()}
	if err := s.eng.PutPart(c.bucket, c.key, u.ID, p); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	c.w.Header().Set("ETag", `"`+p.ETag+`"`)
	if u.ChecksumAlgo != "" {
		c.w.Header().Set("x-amz-checksum-"+u.ChecksumAlgo, cs[u.ChecksumAlgo])
	} else {
		writeChecksumHeaders(c.w.Header(), cs, "")
	}
	c.w.WriteHeader(http.StatusOK)
}

func (s *Server) uploadPartCopy(c *reqCtx, q url.Values) {
	n, aerr := partNumberOf(q)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	srcBucket, srcKey, aerr := copySource(c.r.Header.Get("X-Amz-Copy-Source"))
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	if !c.auth.cred.allows(srcBucket) {
		s.fail(c, errAccessDenied)
		return
	}
	u, err := s.eng.GetUpload(c.bucket, c.key, q.Get("uploadId"))
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	var algos []string
	if u.ChecksumAlgo != "" {
		algos = []string{u.ChecksumAlgo}
	}
	_, seg, sum, cs, aerr := s.copyBytes(c, srcBucket, srcKey, c.r.Header.Get("X-Amz-Copy-Source-Range"), algos)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	p := engine.Part{Number: n, Blob: seg.Blob, Size: seg.Size, ETag: hex.EncodeToString(sum), MD5: sum, Checksum: cs,
		Modified: s.now().UTC()}
	if err := s.eng.PutPart(c.bucket, c.key, u.ID, p); err != nil {
		s.fail(c, mapErr(err))
		return
	}
	s.xml(c, http.StatusOK, copyPartResult{Xmlns: xmlns, LastModified: isoTime(p.Modified), ETag: `"` + p.ETag + `"`})
}

func partChecksums(p engine.Part) partXML {
	x := partXML{PartNumber: p.Number, LastModified: isoTime(p.Modified), ETag: `"` + p.ETag + `"`, Size: p.Size}
	for a, v := range p.Checksum {
		switch a {
		case "crc32":
			x.ChecksumCRC32 = v
		case "crc32c":
			x.ChecksumCRC32C = v
		case "crc64nvme":
			x.ChecksumCRC64NVME = v
		case "sha1":
			x.ChecksumSHA1 = v
		case "sha256":
			x.ChecksumSHA256 = v
		}
	}
	return x
}

func (s *Server) listParts(c *reqCtx, q url.Values) {
	u, err := s.eng.GetUpload(c.bucket, c.key, q.Get("uploadId"))
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	after, _ := strconv.Atoi(q.Get("part-number-marker"))
	max := 1000
	if v := q.Get("max-parts"); v != "" {
		m, err := strconv.Atoi(v)
		if err != nil || m < 0 {
			s.fail(c, errInvalidArgument)
			return
		}
		if m < max {
			max = m
		}
	}
	parts, truncated, err := s.eng.ListParts(c.bucket, c.key, u.ID, after, max)
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	o := owner{ID: c.auth.cred.AccessKey, DisplayName: c.auth.cred.AccessKey}
	out := listPartsResult{Xmlns: xmlns, Bucket: c.bucket, Key: c.key, UploadID: u.ID, Initiator: o, Owner: o,
		StorageClass: "STANDARD", PartNumberMarker: after, MaxParts: max, IsTruncated: truncated,
		ChecksumAlgorithm: strings.ToUpper(u.ChecksumAlgo)}
	for _, p := range parts {
		out.Parts = append(out.Parts, partChecksums(p))
		out.NextPartNumberMarker = p.Number
	}
	s.xml(c, http.StatusOK, out)
}

func (s *Server) completeUpload(c *reqCtx, q url.Values) {
	id := q.Get("uploadId")
	data, aerr := s.readSmallBody(c, 8<<20)
	if aerr != nil {
		s.fail(c, aerr)
		return
	}
	var req completeRequest
	if xml.Unmarshal(data, &req) != nil || len(req.Parts) == 0 {
		s.fail(c, errMalformedXML)
		return
	}
	u, err := s.eng.GetUpload(c.bucket, c.key, id)
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	all, _, err := s.eng.ListParts(c.bucket, c.key, id, 0, engine.MaxParts)
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	have := map[int]engine.Part{}
	for _, p := range all {
		have[p.Number] = p
	}
	var want []engine.CompletePart
	var chosen []engine.Part
	for _, rp := range req.Parts {
		want = append(want, engine.CompletePart{Number: rp.PartNumber, ETag: rp.ETag})
		p, ok := have[rp.PartNumber]
		if !ok {
			s.fail(c, errInvalidPart)
			return
		}
		given := map[string]string{"crc32": rp.ChecksumCRC32, "crc32c": rp.ChecksumCRC32C, "crc64nvme": rp.ChecksumCRC64NVME,
			"sha1": rp.ChecksumSHA1, "sha256": rp.ChecksumSHA256}
		for a, v := range given {
			if v != "" && p.Checksum[a] != "" && p.Checksum[a] != v {
				s.fail(c, errInvalidPart)
				return
			}
		}
		chosen = append(chosen, p)
	}
	// Object checksum: composite (checksum of the part checksums) or over the whole object.
	var checksums map[string]string
	ctype := u.ChecksumType
	if a := u.ChecksumAlgo; a != "" {
		h := newChecksum(a)
		if ctype == "COMPOSITE" {
			for _, p := range chosen {
				raw, err := base64.StdEncoding.DecodeString(p.Checksum[a])
				if err != nil || len(raw) == 0 {
					s.fail(c, errInvalidPart)
					return
				}
				h.Write(raw)
			}
			checksums = map[string]string{a: base64.StdEncoding.EncodeToString(h.Sum(nil)) + "-" + strconv.Itoa(len(chosen))}
		} else {
			var segs []engine.Segment
			var size int64
			for _, p := range chosen {
				segs = append(segs, engine.Segment{Blob: p.Blob, Size: p.Size})
				size += p.Size
			}
			rc, err := s.eng.Open(engine.Object{Segments: segs}, 0, size)
			if err != nil {
				s.fail(c, errInternal)
				return
			}
			_, err = io.Copy(h, rc)
			rc.Close()
			if err != nil {
				s.fail(c, errInternal)
				return
			}
			checksums = map[string]string{a: base64.StdEncoding.EncodeToString(h.Sum(nil))}
		}
		if v := c.r.Header.Get("X-Amz-Checksum-" + a); v != "" && v != checksums[a] && ctype == "FULL_OBJECT" {
			s.fail(c, errChecksumMismatchErr)
			return
		}
	}
	cond := engine.Condition{IfMatch: c.r.Header.Get("If-Match"), IfNoneMatch: c.r.Header.Get("If-None-Match")}
	obj, err := s.eng.CompleteUpload(c.bucket, c.key, id, want, cond, func(_ engine.Upload, parts []engine.Part) (map[string]string, string, error) {
		if len(parts) != len(chosen) {
			return nil, "", engine.ErrInvalidPart
		}
		for i := range parts {
			if parts[i].Blob != chosen[i].Blob {
				return nil, "", engine.ErrConflict // a part was re-uploaded while completing
			}
		}
		return checksums, ctype, nil
	})
	if err != nil {
		s.fail(c, mapErr(err))
		return
	}
	res := completeResult{Xmlns: xmlns, Location: "/" + c.bucket + "/" + c.key, Bucket: c.bucket, Key: c.key,
		ETag: `"` + obj.ETag + `"`}
	for a, v := range checksums {
		switch a {
		case "crc32":
			res.ChecksumCRC32 = v
		case "crc32c":
			res.ChecksumCRC32C = v
		case "crc64nvme":
			res.ChecksumCRC64NVME = v
		case "sha1":
			res.ChecksumSHA1 = v
		case "sha256":
			res.ChecksumSHA256 = v
		}
		res.ChecksumType = ctype
	}
	s.xml(c, http.StatusOK, res)
}
