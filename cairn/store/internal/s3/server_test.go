package s3

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
)

// AWS's published example of a SigV4 streaming upload (s3/latest/API/sigv4-streaming.html): the same
// credentials, request and chunk signatures must verify here.
const (
	exampleAK     = "AKIAIOSFODNN7EXAMPLE"
	exampleSK     = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
	exampleSeed   = "4f232c4386841ef735655705268965c44a0e4690baa4adea153f7db9fa80a0a9"
	exampleChunk1 = "ad80c730a21e5b8d04586a2213dd63b9a0e99e0e2307b0ade35a65485a288648"
	exampleChunk2 = "0055627c9e194cb4542bae2aa5492e3c1575bbb81b612b7d234b86a503ef5497"
	exampleFinal  = "b6c6ea8a5354eaf15b3cb7646744f4275b71ea724fed81ceb9323e279d449df9"
)

func exampleServer(t *testing.T) (*Server, *engine.Engine) {
	t.Helper()
	eng, err := engine.Open(t.TempDir(), engine.Options{Fsync: false})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { eng.Close() })
	if err := eng.CreateBucket("examplebucket"); err != nil {
		t.Fatal(err)
	}
	srv, err := New(eng, Config{Credentials: []Credential{{AccessKey: exampleAK, SecretKey: exampleSK, Admin: true}}})
	if err != nil {
		t.Fatal(err)
	}
	srv.SetClock(func() time.Time { return time.Date(2013, 5, 24, 0, 0, 30, 0, time.UTC) })
	return srv, eng
}

func exampleRequest(chunk2Sig string) *http.Request {
	var body strings.Builder
	fmt.Fprintf(&body, "10000;chunk-signature=%s\r\n%s\r\n", exampleChunk1, strings.Repeat("a", 65536))
	fmt.Fprintf(&body, "400;chunk-signature=%s\r\n%s\r\n", chunk2Sig, strings.Repeat("a", 1024))
	fmt.Fprintf(&body, "0;chunk-signature=%s\r\n\r\n", exampleFinal)
	r := httptest.NewRequest(http.MethodPut, "http://s3.amazonaws.com/examplebucket/chunkObject.txt", strings.NewReader(body.String()))
	r.Host = "s3.amazonaws.com"
	r.ContentLength = int64(body.Len())
	r.Header.Set("Content-Length", fmt.Sprint(body.Len()))
	r.Header.Set("X-Amz-Date", "20130524T000000Z")
	r.Header.Set("X-Amz-Storage-Class", "REDUCED_REDUNDANCY")
	r.Header.Set("X-Amz-Content-Sha256", streamingSigned)
	r.Header.Set("Content-Encoding", "aws-chunked")
	r.Header.Set("X-Amz-Decoded-Content-Length", "66560")
	r.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+exampleAK+"/20130524/us-east-1/s3/aws4_request,"+
		"SignedHeaders=content-encoding;content-length;host;x-amz-content-sha256;x-amz-date;x-amz-decoded-content-length;x-amz-storage-class,"+
		"Signature="+exampleSeed)
	return r
}

func TestAWSStreamingExampleVerifies(t *testing.T) {
	srv, eng := exampleServer(t)
	w := httptest.NewRecorder()
	srv.ServeHTTP(w, exampleRequest(exampleChunk2))
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	obj, err := eng.GetObject("examplebucket", "chunkObject.txt")
	if err != nil {
		t.Fatal(err)
	}
	if obj.Size != 66560 {
		t.Fatalf("decoded size %d", obj.Size)
	}
	rc, _ := eng.Open(obj, 0, obj.Size)
	b, _ := io.ReadAll(rc)
	rc.Close()
	if string(b) != strings.Repeat("a", 66560) || obj.Headers["Content-Encoding"] != "" {
		t.Fatalf("stored %d bytes, encoding %q", len(b), obj.Headers["Content-Encoding"])
	}
}

func TestTamperedChunkIsRejectedAndNothingStored(t *testing.T) {
	srv, eng := exampleServer(t)
	w := httptest.NewRecorder()
	srv.ServeHTTP(w, exampleRequest(strings.Repeat("0", 64)))
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "SignatureDoesNotMatch") {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if _, err := eng.GetObject("examplebucket", "chunkObject.txt"); err != engine.ErrNoSuchKey {
		t.Fatalf("object must not exist: %v", err)
	}
}

func TestUnsignedRequestsAreRefused(t *testing.T) {
	srv, _ := exampleServer(t)
	w := httptest.NewRecorder()
	srv.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "http://localhost/examplebucket/x", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("anonymous read allowed: %d", w.Code)
	}
	w = httptest.NewRecorder()
	srv.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "http://localhost/-/healthy", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("health %d", w.Code)
	}
}

func TestRangeParsing(t *testing.T) {
	cases := []struct {
		h          string
		size       int64
		off, n     int64
		ok, errRng bool
	}{
		{"bytes=0-9", 100, 0, 10, true, false},
		{"bytes=90-", 100, 90, 10, true, false},
		{"bytes=-10", 100, 90, 10, true, false},
		{"bytes=95-200", 100, 95, 5, true, false},
		{"bytes=100-", 100, 0, 0, false, true},
		{"bytes=-500", 100, 0, 100, true, false},
		{"bytes=5-2", 100, 0, 100, false, false},
		{"items=0-1", 100, 0, 100, false, false},
		{"bytes=0-1,4-5", 100, 0, 100, false, false},
		{"bytes=0-0", 0, 0, 0, false, true},
	}
	for _, c := range cases {
		off, n, ok, aerr := parseRange(c.h, c.size)
		if (aerr != nil) != c.errRng || (aerr == nil && (off != c.off || n != c.n || ok != c.ok)) {
			t.Errorf("%s/%d: got off=%d n=%d ok=%v err=%v", c.h, c.size, off, n, ok, aerr)
		}
	}
}

func TestCRC64NVMECheckValue(t *testing.T) {
	h := newChecksum("crc64nvme")
	h.Write([]byte("123456789"))
	if got := fmt.Sprintf("%x", h.Sum(nil)); got != "ae8b14860a799888" {
		t.Fatalf("CRC-64/NVME check value: %s", got)
	}
}
