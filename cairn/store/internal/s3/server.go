// Package s3 serves the S3 API over a Cairn Store engine.
package s3

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
)

// Config configures the S3 server.
type Config struct {
	Region      string
	Domain      string // enables virtual-hosted-style requests: <bucket>.<Domain>
	Credentials []Credential
	MaxObject   int64 // largest single PUT (default 5 GiB)
	Logger      *slog.Logger
}

// Server is an http.Handler speaking S3.
type Server struct {
	eng     *engine.Engine
	creds   map[string]Credential
	region  string
	domain  string
	maxObj  int64
	log     *slog.Logger
	now     func() time.Time
	metrics *metrics
}

// New builds a server.
func New(eng *engine.Engine, cfg Config) (*Server, error) {
	if len(cfg.Credentials) == 0 {
		return nil, errors.New("at least one credential is required")
	}
	s := &Server{eng: eng, creds: map[string]Credential{}, region: cfg.Region, domain: strings.ToLower(cfg.Domain),
		maxObj: cfg.MaxObject, log: cfg.Logger, now: time.Now, metrics: newMetrics()}
	if s.region == "" {
		s.region = "us-east-1"
	}
	if s.maxObj <= 0 {
		s.maxObj = 5 << 30
	}
	if s.log == nil {
		s.log = slog.Default()
	}
	for _, c := range cfg.Credentials {
		if c.AccessKey == "" || len(c.SecretKey) < 8 {
			return nil, fmt.Errorf("credential %q: access key and a secret of at least 8 characters are required", c.AccessKey)
		}
		s.creds[c.AccessKey] = c
	}
	return s, nil
}

// SetClock replaces the clock (tests).
func (s *Server) SetClock(now func() time.Time) { s.now = now }

type reqCtx struct {
	w      *statusWriter
	r      *http.Request
	id     string
	bucket string
	key    string
	auth   *authResult
	op     string
}

type statusWriter struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (w *statusWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(b)
	w.bytes += int64(n)
	return n, err
}

func requestID() string {
	var b [8]byte
	rand.Read(b[:])
	return strings.ToUpper(hex.EncodeToString(b[:]))
}

func (s *Server) ServeHTTP(rw http.ResponseWriter, r *http.Request) {
	start := time.Now()
	w := &statusWriter{ResponseWriter: rw}
	c := &reqCtx{w: w, r: r, id: requestID()}
	h := w.Header()
	h.Set("x-amz-request-id", c.id)
	h.Set("x-amz-id-2", c.id)
	h.Set("Server", "CairnStore")
	defer func() {
		if p := recover(); p != nil {
			s.log.Error("panic", "err", p, "path", r.URL.Path)
			s.fail(c, errInternal)
		}
		s.metrics.observe(c.op, w.status, time.Since(start), r.ContentLength, w.bytes)
		s.log.Debug("request", "id", c.id, "op", c.op, "method", r.Method, "bucket", c.bucket, "key", c.key,
			"status", w.status, "ms", time.Since(start).Milliseconds())
	}()
	if strings.HasPrefix(r.URL.Path, "/-/") {
		c.op = "admin"
		s.admin(c)
		return
	}
	s.route(c)
}

func (s *Server) route(c *reqCtx) {
	r := c.r
	path := r.URL.Path
	host := strings.ToLower(r.Host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	if s.domain != "" && strings.HasSuffix(host, "."+s.domain) {
		c.bucket = strings.TrimSuffix(host, "."+s.domain)
		c.key = strings.TrimPrefix(path, "/")
	} else {
		p := strings.TrimPrefix(path, "/")
		c.bucket, c.key, _ = strings.Cut(p, "/")
	}
	auth, aerr := s.verify(r)
	if aerr != nil {
		c.op = "auth"
		s.fail(c, aerr)
		return
	}
	c.auth = auth
	if c.bucket != "" && !auth.cred.allows(c.bucket) {
		s.fail(c, errAccessDenied)
		return
	}
	if auth.cred.ReadOnly && r.Method != http.MethodGet && r.Method != http.MethodHead {
		s.fail(c, errAccessDenied)
		return
	}
	q := r.URL.Query()
	if c.bucket == "" {
		c.op = "ListBuckets"
		if r.Method != http.MethodGet {
			s.fail(c, errMethodNotAllowed)
			return
		}
		s.listBuckets(c)
		return
	}
	if c.key == "" {
		s.bucketOp(c, q)
		return
	}
	if len(c.key) > 1024 {
		s.fail(c, errKeyTooLong)
		return
	}
	if !utf8.ValidString(c.key) {
		s.fail(c, errInvalidArgument)
		return
	}
	s.objectOp(c, q)
}

func (s *Server) bucketOp(c *reqCtx, q url.Values) {
	switch c.r.Method {
	case http.MethodPut:
		switch {
		case has(q, "acl"), has(q, "versioning"), has(q, "tagging"), has(q, "cors"), has(q, "policy"),
			has(q, "lifecycle"), has(q, "ownershipControls"), has(q, "publicAccessBlock"):
			c.op = "PutBucketConfig"
			if _, err := s.eng.HeadBucket(c.bucket); err != nil {
				s.fail(c, mapErr(err))
				return
			}
			c.w.WriteHeader(http.StatusOK)
		default:
			c.op = "CreateBucket"
			s.createBucket(c)
		}
	case http.MethodDelete:
		switch {
		case has(q, "policy"), has(q, "tagging"), has(q, "cors"), has(q, "lifecycle"):
			c.op = "DeleteBucketConfig"
			c.w.WriteHeader(http.StatusNoContent)
		default:
			c.op = "DeleteBucket"
			s.deleteBucket(c)
		}
	case http.MethodHead:
		c.op = "HeadBucket"
		if _, err := s.eng.HeadBucket(c.bucket); err != nil {
			s.fail(c, mapErr(err))
			return
		}
		c.w.Header().Set("x-amz-bucket-region", s.region)
		c.w.WriteHeader(http.StatusOK)
	case http.MethodPost:
		if has(q, "delete") {
			c.op = "DeleteObjects"
			s.deleteObjects(c)
			return
		}
		s.fail(c, errNotImplemented)
	case http.MethodGet:
		if _, err := s.eng.HeadBucket(c.bucket); err != nil {
			s.fail(c, mapErr(err))
			return
		}
		switch {
		case has(q, "location"):
			c.op = "GetBucketLocation"
			v := s.region
			if v == "us-east-1" {
				v = ""
			}
			s.xml(c, http.StatusOK, locationConstraint{Xmlns: xmlns, Value: v})
		case has(q, "versioning"):
			c.op = "GetBucketVersioning"
			s.xml(c, http.StatusOK, versioningConfiguration{Xmlns: xmlns})
		case has(q, "acl"):
			c.op = "GetBucketAcl"
			s.xml(c, http.StatusOK, s.acl(c))
		case has(q, "uploads"):
			c.op = "ListMultipartUploads"
			s.listUploads(c, q)
		case has(q, "policy"):
			s.fail(c, errNoSuchBucketPolicy)
		case has(q, "lifecycle"):
			s.fail(c, errNoSuchLifecycle)
		case has(q, "cors"):
			s.fail(c, errNoSuchCORS)
		case has(q, "tagging"):
			s.fail(c, errNoSuchTagSet)
		case has(q, "versions"), has(q, "object-lock"), has(q, "replication"), has(q, "encryption"),
			has(q, "website"), has(q, "logging"), has(q, "notification"), has(q, "requestPayment"):
			c.op = "Unsupported"
			s.fail(c, errNotImplemented)
		case q.Get("list-type") == "2":
			c.op = "ListObjectsV2"
			s.listV2(c, q)
		default:
			c.op = "ListObjects"
			s.listV1(c, q)
		}
	default:
		s.fail(c, errMethodNotAllowed)
	}
}

func (s *Server) objectOp(c *reqCtx, q url.Values) {
	r := c.r
	switch r.Method {
	case http.MethodPut:
		switch {
		case has(q, "uploadId") && has(q, "partNumber"):
			if r.Header.Get("X-Amz-Copy-Source") != "" {
				c.op = "UploadPartCopy"
				s.uploadPartCopy(c, q)
			} else {
				c.op = "UploadPart"
				s.uploadPart(c, q)
			}
		case has(q, "acl"), has(q, "tagging"), has(q, "retention"), has(q, "legal-hold"):
			c.op = "PutObjectConfig"
			if _, err := s.eng.GetObject(c.bucket, c.key); err != nil {
				s.fail(c, mapErr(err))
				return
			}
			c.w.WriteHeader(http.StatusOK)
		case r.Header.Get("X-Amz-Copy-Source") != "":
			c.op = "CopyObject"
			s.copyObject(c)
		default:
			c.op = "PutObject"
			s.putObject(c)
		}
	case http.MethodGet:
		switch {
		case has(q, "uploadId"):
			c.op = "ListParts"
			s.listParts(c, q)
		case has(q, "acl"):
			c.op = "GetObjectAcl"
			if _, err := s.eng.GetObject(c.bucket, c.key); err != nil {
				s.fail(c, mapErr(err))
				return
			}
			s.xml(c, http.StatusOK, s.acl(c))
		case has(q, "tagging"):
			c.op = "GetObjectTagging"
			if _, err := s.eng.GetObject(c.bucket, c.key); err != nil {
				s.fail(c, mapErr(err))
				return
			}
			c.w.Header().Set("Content-Type", "application/xml")
			c.w.WriteHeader(http.StatusOK)
			fmt.Fprintf(c.w, `<?xml version="1.0" encoding="UTF-8"?><Tagging xmlns="%s"><TagSet></TagSet></Tagging>`, xmlns)
		case has(q, "attributes"), has(q, "torrent"), has(q, "retention"), has(q, "legal-hold"):
			s.fail(c, errNotImplemented)
		default:
			c.op = "GetObject"
			s.getObject(c, q, false)
		}
	case http.MethodHead:
		c.op = "HeadObject"
		s.getObject(c, q, true)
	case http.MethodDelete:
		if has(q, "uploadId") {
			c.op = "AbortMultipartUpload"
			if err := s.eng.AbortUpload(c.bucket, c.key, q.Get("uploadId")); err != nil {
				s.fail(c, mapErr(err))
				return
			}
			c.w.WriteHeader(http.StatusNoContent)
			return
		}
		if has(q, "tagging") {
			c.w.WriteHeader(http.StatusNoContent)
			return
		}
		c.op = "DeleteObject"
		if err := s.eng.DeleteObject(c.bucket, c.key, engine.Condition{IfMatch: r.Header.Get("If-Match")}); err != nil {
			s.fail(c, mapErr(err))
			return
		}
		c.w.WriteHeader(http.StatusNoContent)
	case http.MethodPost:
		switch {
		case has(q, "uploads"):
			c.op = "CreateMultipartUpload"
			s.createUpload(c)
		case has(q, "uploadId"):
			c.op = "CompleteMultipartUpload"
			s.completeUpload(c, q)
		default:
			s.fail(c, errNotImplemented)
		}
	default:
		s.fail(c, errMethodNotAllowed)
	}
}

func has(q url.Values, k string) bool {
	_, ok := q[k]
	return ok
}

// ---------------------------------------------------------------------------------------------- responses

func (s *Server) xml(c *reqCtx, status int, v any) {
	out, err := xml.Marshal(v)
	if err != nil {
		s.fail(c, errInternal)
		return
	}
	c.w.Header().Set("Content-Type", "application/xml")
	c.w.WriteHeader(status)
	c.w.Write([]byte(xml.Header))
	c.w.Write(out)
}

func (s *Server) fail(c *reqCtx, e *apiError) {
	if c.w.status != 0 {
		return
	}
	if c.r.Method == http.MethodHead {
		c.w.WriteHeader(e.Status)
		return
	}
	resource := c.r.URL.Path
	out, _ := xml.Marshal(errorBody{Code: e.Code, Message: e.Message, Resource: resource, RequestID: c.id})
	c.w.Header().Set("Content-Type", "application/xml")
	c.w.WriteHeader(e.Status)
	c.w.Write([]byte(xml.Header))
	c.w.Write(out)
}

func mapErr(err error) *apiError {
	var ae *apiError
	switch {
	case errors.As(err, &ae):
		return ae
	case errors.Is(err, engine.ErrNoSuchBucket):
		return errNoSuchBucket
	case errors.Is(err, engine.ErrNoSuchKey):
		return errNoSuchKey
	case errors.Is(err, engine.ErrNoSuchUpload):
		return errNoSuchUpload
	case errors.Is(err, engine.ErrBucketExists):
		return errBucketOwned
	case errors.Is(err, engine.ErrBucketNotEmpty):
		return errBucketNotEmpty
	case errors.Is(err, engine.ErrPreconditionFails):
		return errPrecondition
	case errors.Is(err, engine.ErrConflict):
		return errConditionalConflict
	case errors.Is(err, engine.ErrInvalidPart):
		return errInvalidPart
	case errors.Is(err, engine.ErrInvalidPartOrder):
		return errInvalidPartOrder
	case errors.Is(err, engine.ErrEntityTooSmall):
		return errEntityTooSmall
	case errors.Is(err, errContentSHA):
		return errContentSHAMismatch
	case errors.Is(err, errChecksumMismatch):
		return errChecksumMismatchErr
	case errors.Is(err, errBadDigest):
		return errBadDigestErr
	case errors.Is(err, errIncompleteBody):
		return errIncompleteBodyErr
	case errors.Is(err, errChunkSignature):
		return errChunkSigErr
	case errors.Is(err, errChunkFormat):
		return errIncompleteBodyErr
	}
	return errInternal
}

func (s *Server) acl(c *reqCtx) accessControlPolicy {
	o := owner{ID: c.auth.cred.AccessKey, DisplayName: c.auth.cred.AccessKey}
	var g grant
	g.Grantee.XMLNS = "http://www.w3.org/2001/XMLSchema-instance"
	g.Grantee.Type = "CanonicalUser"
	g.Grantee.ID = o.ID
	g.Grantee.DisplayName = o.DisplayName
	g.Permission = "FULL_CONTROL"
	return accessControlPolicy{Xmlns: xmlns, Owner: o, Grants: []grant{g}}
}

func isoTime(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }

// ---------------------------------------------------------------------------------------------- admin

func (s *Server) admin(c *reqCtx) {
	switch c.r.URL.Path {
	case "/-/healthy":
		c.w.WriteHeader(http.StatusOK)
		c.w.Write([]byte("ok\n"))
	case "/-/ready":
		if _, err := s.eng.ListBuckets(); err != nil {
			c.w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		c.w.WriteHeader(http.StatusOK)
		c.w.Write([]byte("ready\n"))
	case "/-/metrics":
		c.w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		c.w.WriteHeader(http.StatusOK)
		s.metrics.write(c.w, s.eng)
	default:
		c.w.WriteHeader(http.StatusNotFound)
	}
}

// ---------------------------------------------------------------------------------------------- metrics

type metrics struct {
	mu       sync.Mutex
	requests map[string]int64 // op|status -> count
	seconds  map[string]float64
	bytesIn  atomic.Int64
	bytesOut atomic.Int64
	statsAt  time.Time
	stats    engine.Stats
}

func newMetrics() *metrics {
	return &metrics{requests: map[string]int64{}, seconds: map[string]float64{}}
}

func (m *metrics) observe(op string, status int, d time.Duration, in, out int64) {
	if op == "" {
		op = "unknown"
	}
	if status == 0 {
		status = 200
	}
	m.mu.Lock()
	k := fmt.Sprintf("%s|%d", op, status)
	m.requests[k]++
	m.seconds[op] += d.Seconds()
	m.mu.Unlock()
	if in > 0 {
		m.bytesIn.Add(in)
	}
	m.bytesOut.Add(out)
}

func (m *metrics) write(w http.ResponseWriter, eng *engine.Engine) {
	m.mu.Lock()
	defer m.mu.Unlock()
	fmt.Fprintln(w, "# TYPE cairn_store_requests_total counter")
	for k, v := range m.requests {
		op, st, _ := strings.Cut(k, "|")
		fmt.Fprintf(w, "cairn_store_requests_total{op=%q,status=%q} %d\n", op, st, v)
	}
	fmt.Fprintln(w, "# TYPE cairn_store_request_seconds_total counter")
	for op, v := range m.seconds {
		fmt.Fprintf(w, "cairn_store_request_seconds_total{op=%q} %g\n", op, v)
	}
	fmt.Fprintf(w, "# TYPE cairn_store_received_bytes_total counter\ncairn_store_received_bytes_total %d\n", m.bytesIn.Load())
	fmt.Fprintf(w, "# TYPE cairn_store_sent_bytes_total counter\ncairn_store_sent_bytes_total %d\n", m.bytesOut.Load())
	if time.Since(m.statsAt) > 30*time.Second {
		if st, err := eng.Stats(); err == nil {
			m.stats, m.statsAt = st, time.Now()
		}
	}
	fmt.Fprintf(w, "# TYPE cairn_store_buckets gauge\ncairn_store_buckets %d\n", m.stats.Buckets)
	fmt.Fprintf(w, "# TYPE cairn_store_objects gauge\ncairn_store_objects %d\n", m.stats.Objects)
	fmt.Fprintf(w, "# TYPE cairn_store_stored_bytes gauge\ncairn_store_stored_bytes %d\n", m.stats.Bytes)
	fmt.Fprintf(w, "# TYPE cairn_store_multipart_uploads gauge\ncairn_store_multipart_uploads %d\n", m.stats.Uploads)
}
