package s3

import (
	"crypto/hmac"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"net/url"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Credential is an access key the store accepts.
type Credential struct {
	AccessKey string   `json:"access_key"`
	SecretKey string   `json:"secret_key"`
	Buckets   []string `json:"buckets,omitempty"`   // glob patterns; empty = all buckets
	ReadOnly  bool     `json:"read_only,omitempty"` // only GET/HEAD/list
	Admin     bool     `json:"admin,omitempty"`     // may create and delete buckets
}

func (c Credential) allows(bucket string) bool {
	if len(c.Buckets) == 0 || bucket == "" {
		return true
	}
	for _, p := range c.Buckets {
		if ok, _ := path.Match(p, bucket); ok {
			return true
		}
	}
	return false
}

const (
	algorithm        = "AWS4-HMAC-SHA256"
	unsignedPayload  = "UNSIGNED-PAYLOAD"
	streamingSigned  = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
	streamingSignedT = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER"
	streamingUnsignT = "STREAMING-UNSIGNED-PAYLOAD-TRAILER"
	emptySHA256      = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	maxSkew          = 15 * time.Minute
)

// authResult is a verified request signature.
type authResult struct {
	cred        Credential
	payloadHash string // hex SHA-256, or one of the UNSIGNED/STREAMING markers
	seedSig     string
	signingKey  []byte
	amzDate     string
	scope       string
}

func hmacSHA256(key []byte, data string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(data))
	return m.Sum(nil)
}

func sha256Hex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func signingKey(secret, date, region, service string) []byte {
	k := hmacSHA256([]byte("AWS4"+secret), date)
	k = hmacSHA256(k, region)
	k = hmacSHA256(k, service)
	return hmacSHA256(k, "aws4_request")
}

// awsEncode is SigV4 URI encoding: unreserved characters stay, everything else is %XX (upper case).
func awsEncode(s string, keepSlash bool) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
			c == '-' || c == '_' || c == '.' || c == '~' || (keepSlash && c == '/') {
			b.WriteByte(c)
		} else {
			b.WriteString("%" + strings.ToUpper(hex.EncodeToString([]byte{c})))
		}
	}
	return b.String()
}

func canonicalURI(r *http.Request) string {
	p := r.URL.EscapedPath()
	if dec, err := url.PathUnescape(p); err == nil {
		p = dec
	}
	if p == "" {
		p = "/"
	}
	return awsEncode(p, true)
}

func canonicalQuery(raw string, skip string) string {
	type kv struct{ k, v string }
	var pairs []kv
	for _, part := range strings.Split(raw, "&") {
		if part == "" {
			continue
		}
		k, v, _ := strings.Cut(part, "=")
		dk, err := url.QueryUnescape(k)
		if err != nil {
			dk = k
		}
		dv, err := url.QueryUnescape(v)
		if err != nil {
			dv = v
		}
		if dk == skip {
			continue
		}
		pairs = append(pairs, kv{awsEncode(dk, false), awsEncode(dv, false)})
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].k != pairs[j].k {
			return pairs[i].k < pairs[j].k
		}
		return pairs[i].v < pairs[j].v
	})
	out := make([]string, len(pairs))
	for i, p := range pairs {
		out[i] = p.k + "=" + p.v
	}
	return strings.Join(out, "&")
}

func canonicalHeaders(r *http.Request, signed []string) string {
	var b strings.Builder
	for _, h := range signed {
		var v string
		switch h {
		case "host":
			v = r.Host
		case "content-length":
			v = r.Header.Get("Content-Length")
			if v == "" && r.ContentLength >= 0 && r.Method != http.MethodGet && r.Method != http.MethodHead {
				v = strconv.FormatInt(r.ContentLength, 10)
			}
		default:
			vals := r.Header.Values(h)
			for i := range vals {
				vals[i] = strings.Join(strings.Fields(vals[i]), " ")
			}
			v = strings.Join(vals, ",")
		}
		b.WriteString(h + ":" + strings.TrimSpace(v) + "\n")
	}
	return b.String()
}

// verify checks a SigV4-signed request (Authorization header or presigned query).
func (s *Server) verify(r *http.Request) (*authResult, *apiError) {
	q := r.URL.Query()
	if h := r.Header.Get("Authorization"); h != "" {
		if !strings.HasPrefix(h, algorithm+" ") {
			if strings.HasPrefix(h, "AWS ") {
				ak, sig, ok := strings.Cut(strings.TrimPrefix(h, "AWS "), ":")
				if !ok {
					return nil, errAuthHeaderMalformed
				}
				date := r.Header.Get("Date")
				if r.Header.Get("X-Amz-Date") != "" {
					date = ""
				}
				if t, err := http.ParseTime(firstNonEmpty(r.Header.Get("X-Amz-Date"), r.Header.Get("Date"))); err != nil ||
					s.now().Sub(t) > maxSkew || t.Sub(s.now()) > maxSkew {
					return nil, errTimeSkewed
				}
				return s.checkV2(r, ak, sig, date)
			}
			return nil, errAuthHeaderMalformed
		}
		fields := map[string]string{}
		for _, part := range strings.Split(strings.TrimPrefix(h, algorithm+" "), ",") {
			k, v, ok := strings.Cut(strings.TrimSpace(part), "=")
			if ok {
				fields[k] = v
			}
		}
		amzDate := r.Header.Get("X-Amz-Date")
		if amzDate == "" {
			if d, err := http.ParseTime(r.Header.Get("Date")); err == nil {
				amzDate = d.UTC().Format("20060102T150405Z")
			}
		}
		payload := r.Header.Get("X-Amz-Content-Sha256")
		if payload == "" {
			return nil, errMissingContentSHA
		}
		return s.check(r, fields["Credential"], fields["SignedHeaders"], fields["Signature"], amzDate, payload, "", 0)
	}
	if q.Get("X-Amz-Algorithm") != "" {
		if q.Get("X-Amz-Algorithm") != algorithm {
			return nil, errAuthQueryMalformed
		}
		expires, err := strconv.Atoi(q.Get("X-Amz-Expires"))
		if err != nil || expires < 0 || expires > 7*24*3600 {
			return nil, errAuthQueryMalformed
		}
		payload := q.Get("X-Amz-Content-Sha256")
		if payload == "" {
			payload = unsignedPayload
		}
		return s.check(r, q.Get("X-Amz-Credential"), q.Get("X-Amz-SignedHeaders"), q.Get("X-Amz-Signature"),
			q.Get("X-Amz-Date"), payload, "X-Amz-Signature", time.Duration(expires)*time.Second)
	}
	if ak := q.Get("AWSAccessKeyId"); ak != "" {
		exp, err := strconv.ParseInt(q.Get("Expires"), 10, 64)
		if err != nil {
			return nil, errAuthQueryMalformed
		}
		if s.now().Unix() > exp {
			return nil, errExpired
		}
		return s.checkV2(r, ak, q.Get("Signature"), q.Get("Expires"))
	}
	return nil, errAccessDenied
}

func (s *Server) check(r *http.Request, credential, signedHeaders, signature, amzDate, payload, skip string,
	expires time.Duration) (*authResult, *apiError) {
	parts := strings.Split(credential, "/")
	if len(parts) != 5 || parts[4] != "aws4_request" || signedHeaders == "" || signature == "" {
		return nil, errAuthHeaderMalformed
	}
	cred, ok := s.creds[parts[0]]
	if !ok {
		return nil, errInvalidAccessKey
	}
	t, err := time.Parse("20060102T150405Z", amzDate)
	if err != nil || t.Format("20060102") != parts[1] {
		return nil, errAuthHeaderMalformed
	}
	now := s.now()
	if expires > 0 {
		if now.After(t.Add(expires)) {
			return nil, errExpired
		}
		if t.After(now.Add(maxSkew)) {
			return nil, errTimeSkewed
		}
	} else if now.Sub(t) > maxSkew || t.Sub(now) > maxSkew {
		return nil, errTimeSkewed
	}
	signed := strings.Split(signedHeaders, ";")
	canonical := strings.Join([]string{
		r.Method,
		canonicalURI(r),
		canonicalQuery(r.URL.RawQuery, skip),
		canonicalHeaders(r, signed),
		signedHeaders,
		payload,
	}, "\n")
	scope := strings.Join(parts[1:], "/")
	toSign := strings.Join([]string{algorithm, amzDate, scope, sha256Hex([]byte(canonical))}, "\n")
	key := signingKey(cred.SecretKey, parts[1], parts[2], parts[3])
	want := hex.EncodeToString(hmacSHA256(key, toSign))
	if subtle.ConstantTimeCompare([]byte(want), []byte(signature)) != 1 {
		return nil, errSignatureMismatch
	}
	return &authResult{cred: cred, payloadHash: payload, seedSig: signature, signingKey: key, amzDate: amzDate,
		scope: scope}, nil
}

func firstNonEmpty(v ...string) string {
	for _, x := range v {
		if x != "" {
			return x
		}
	}
	return ""
}

// Sub-resources that are part of a SigV2 canonical resource.
var v2SubResources = map[string]bool{"acl": true, "cors": true, "delete": true, "lifecycle": true, "location": true,
	"logging": true, "notification": true, "partNumber": true, "policy": true, "requestPayment": true, "restore": true,
	"tagging": true, "torrent": true, "uploadId": true, "uploads": true, "versionId": true, "versioning": true,
	"versions": true, "website": true, "response-cache-control": true, "response-content-disposition": true,
	"response-content-encoding": true, "response-content-language": true, "response-content-type": true,
	"response-expires": true}

// checkV2 verifies a Signature Version 2 request (header or presigned query). Still sent by tools such as
// boto3 when presigning URLs.
func (s *Server) checkV2(r *http.Request, ak, sig, date string) (*authResult, *apiError) {
	cred, ok := s.creds[ak]
	if !ok {
		return nil, errInvalidAccessKey
	}
	var amz []string
	for k, v := range r.Header {
		lk := strings.ToLower(k)
		if strings.HasPrefix(lk, "x-amz-") {
			amz = append(amz, lk+":"+strings.Join(v, ","))
		}
	}
	sort.Strings(amz)
	resource := r.URL.EscapedPath()
	if s.domain != "" {
		host := strings.ToLower(r.Host)
		if i := strings.LastIndex(host, ":"); i >= 0 {
			host = host[:i]
		}
		if strings.HasSuffix(host, "."+s.domain) {
			resource = "/" + strings.TrimSuffix(host, "."+s.domain) + resource
		}
	}
	var subs []string
	for k, vs := range r.URL.Query() {
		if !v2SubResources[k] {
			continue
		}
		if len(vs) == 0 || vs[0] == "" {
			subs = append(subs, k)
		} else {
			subs = append(subs, k+"="+vs[0])
		}
	}
	sort.Strings(subs)
	if len(subs) > 0 {
		resource += "?" + strings.Join(subs, "&")
	}
	var b strings.Builder
	b.WriteString(r.Method + "\n" + r.Header.Get("Content-Md5") + "\n" + r.Header.Get("Content-Type") + "\n" + date + "\n")
	for _, h := range amz {
		b.WriteString(h + "\n")
	}
	b.WriteString(resource)
	m := hmac.New(sha1.New, []byte(cred.SecretKey))
	m.Write([]byte(b.String()))
	want := base64.StdEncoding.EncodeToString(m.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(want), []byte(sig)) != 1 {
		return nil, errSignatureMismatch
	}
	return &authResult{cred: cred, payloadHash: unsignedPayload}, nil
}
