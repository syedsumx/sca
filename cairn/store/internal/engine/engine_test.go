package engine

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func open(t *testing.T, dir string) *Engine {
	t.Helper()
	e, err := Open(dir, Options{Fsync: true})
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func put(t *testing.T, e *Engine, bucket, key, data string, cond Condition) error {
	t.Helper()
	w, err := e.NewBlob()
	if err != nil {
		t.Fatal(err)
	}
	io.WriteString(w, data)
	seg, err := w.Seal()
	if err != nil {
		t.Fatal(err)
	}
	return e.PutObject(bucket, Object{Key: key, Size: seg.Size, ETag: fmt.Sprintf("%x", w.MD5()), Modified: time.Now(),
		Segments: []Segment{seg}}, cond)
}

func read(t *testing.T, e *Engine, bucket, key string) string {
	t.Helper()
	o, err := e.GetObject(bucket, key)
	if err != nil {
		t.Fatal(err)
	}
	rc, err := e.Open(o, 0, o.Size)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	b, _ := io.ReadAll(rc)
	return string(b)
}

func blobCount(t *testing.T, dir string) int {
	n := 0
	filepath.WalkDir(filepath.Join(dir, "blobs"), func(_ string, d os.DirEntry, _ error) error {
		if d != nil && !d.IsDir() {
			n++
		}
		return nil
	})
	return n
}

func TestReplaceAndDeleteCollectOldBlobs(t *testing.T) {
	dir := t.TempDir()
	e := open(t, dir)
	defer e.Close()
	if err := e.CreateBucket("b"); err != nil {
		t.Fatal(err)
	}
	if err := e.CreateBucket("b"); !errors.Is(err, ErrBucketExists) {
		t.Fatalf("want exists, got %v", err)
	}
	put(t, e, "b", "k", "one", Condition{})
	put(t, e, "b", "k", "two", Condition{})
	if got := read(t, e, "b", "k"); got != "two" {
		t.Fatalf("read %q", got)
	}
	if n := blobCount(t, dir); n != 1 {
		t.Fatalf("old blob not collected: %d blobs", n)
	}
	if err := e.DeleteBucket("b"); !errors.Is(err, ErrBucketNotEmpty) {
		t.Fatalf("want not empty, got %v", err)
	}
	if err := e.DeleteObject("b", "k", Condition{}); err != nil {
		t.Fatal(err)
	}
	if err := e.DeleteObject("b", "k", Condition{}); err != nil {
		t.Fatal("deleting a missing key must succeed")
	}
	if n := blobCount(t, dir); n != 0 {
		t.Fatalf("%d blobs left", n)
	}
	if err := e.DeleteBucket("b"); err != nil {
		t.Fatal(err)
	}
}

func TestConditionalCreateHasExactlyOneWinner(t *testing.T) {
	dir := t.TempDir()
	e := open(t, dir)
	defer e.Close()
	e.CreateBucket("b")
	var wins, losses atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			err := put(t, e, "b", "metadata/v1.json", fmt.Sprintf("writer %d", i), Condition{IfNoneMatch: "*"})
			switch {
			case err == nil:
				wins.Add(1)
			case errors.Is(err, ErrPreconditionFails):
				losses.Add(1)
			default:
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	if wins.Load() != 1 || losses.Load() != 31 {
		t.Fatalf("wins %d losses %d", wins.Load(), losses.Load())
	}
	if n := blobCount(t, dir); n != 1 {
		t.Fatalf("losing writers left %d blobs", n)
	}
	o, _ := e.GetObject("b", "metadata/v1.json")
	if err := put(t, e, "b", "metadata/v1.json", "x", Condition{IfMatch: "nope"}); !errors.Is(err, ErrPreconditionFails) {
		t.Fatalf("if-match mismatch: %v", err)
	}
	if err := put(t, e, "b", "metadata/v1.json", "x", Condition{IfMatch: `"` + o.ETag + `"`}); err != nil {
		t.Fatalf("if-match: %v", err)
	}
	if err := put(t, e, "b", "missing", "x", Condition{IfMatch: "*"}); !errors.Is(err, ErrNoSuchKey) {
		t.Fatalf("if-match on missing key: %v", err)
	}
}

func TestRecoveryAfterCrash(t *testing.T) {
	dir := t.TempDir()
	e := open(t, dir)
	e.CreateBucket("b")
	put(t, e, "b", "kept", "safe", Condition{})
	// Crash 1: a write in progress (temporary file only).
	w, _ := e.NewBlob()
	io.WriteString(w, "half-written")
	// Crash 2: a blob sealed but never committed to the index.
	w2, _ := e.NewBlob()
	io.WriteString(w2, "orphan")
	if _, err := w2.Seal(); err != nil {
		t.Fatal(err)
	}
	e.Close() // the process dies here: neither write is visible
	e = open(t, dir)
	defer e.Close()
	rep, err := e.Recover()
	if err != nil {
		t.Fatal(err)
	}
	if rep.MissingBlobs != 0 || rep.ReferencedNow != 1 {
		t.Fatalf("report after reopen: %+v", rep)
	}
	if got := read(t, e, "b", "kept"); got != "safe" {
		t.Fatalf("committed object lost: %q", got)
	}
	if n := blobCount(t, dir); n != 1 {
		t.Fatalf("orphan blob not removed: %d blobs", n)
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "tmp"))
	if len(entries) != 0 {
		t.Fatalf("temporary files left: %d", len(entries))
	}
	// Lost data is reported, not hidden.
	o, _ := e.GetObject("b", "kept")
	os.Remove(e.blobPath(o.Segments[0].Blob))
	rep, _ = e.Recover()
	if rep.MissingBlobs != 1 {
		t.Fatalf("missing blob not reported: %+v", rep)
	}
}

func TestListingWithDelimiterAndPages(t *testing.T) {
	e := open(t, t.TempDir())
	defer e.Close()
	e.CreateBucket("b")
	keys := []string{"a/1", "a/2", "a/b/1", "a/b/2", "a/c", "b", "c/x/y", "c/z"}
	for _, k := range keys {
		put(t, e, "b", k, k, Condition{})
	}
	res, err := e.List("b", ListOptions{Prefix: "a/", Delimiter: "/", MaxKeys: 1000})
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, o := range res.Objects {
		got = append(got, o.Key)
	}
	if strings.Join(got, ",") != "a/1,a/2,a/c" || strings.Join(res.CommonPrefixes, ",") != "a/b/" {
		t.Fatalf("got %v %v", got, res.CommonPrefixes)
	}
	// Page through everything one entry at a time with a delimiter; each entry exactly once, in order.
	var seen []string
	after := ""
	for i := 0; i < 20; i++ {
		res, err := e.List("b", ListOptions{Delimiter: "/", After: after, MaxKeys: 1})
		if err != nil {
			t.Fatal(err)
		}
		for _, o := range res.Objects {
			seen = append(seen, o.Key)
		}
		seen = append(seen, res.CommonPrefixes...)
		if !res.Truncated {
			break
		}
		after = res.NextAfter
	}
	if strings.Join(seen, ",") != "a/,b,c/" {
		t.Fatalf("paged %v", seen)
	}
	res, _ = e.List("b", ListOptions{Prefix: "c/", MaxKeys: 1000})
	if len(res.Objects) != 2 {
		t.Fatalf("prefix list %d", len(res.Objects))
	}
	if _, err := e.List("missing", ListOptions{MaxKeys: 1}); !errors.Is(err, ErrNoSuchBucket) {
		t.Fatalf("missing bucket: %v", err)
	}
}

func TestMultipartAssemblesPartsWithoutCopying(t *testing.T) {
	dir := t.TempDir()
	e := open(t, dir)
	defer e.Close()
	e.CreateBucket("b")
	u, err := e.CreateUpload(Upload{Bucket: "b", Key: "big"})
	if err != nil {
		t.Fatal(err)
	}
	part := func(n int, data []byte) Part {
		w, _ := e.NewBlob()
		w.Write(data)
		seg, _ := w.Seal()
		p := Part{Number: n, Blob: seg.Blob, Size: seg.Size, ETag: fmt.Sprintf("%x", w.MD5()), MD5: w.MD5()}
		if err := e.PutPart("b", "big", u.ID, p); err != nil {
			t.Fatal(err)
		}
		return p
	}
	p1 := part(1, bytes.Repeat([]byte("a"), MinPartSize))
	part(2, []byte("unused"))
	p2 := part(2, bytes.Repeat([]byte("b"), MinPartSize)) // re-uploaded part replaces the first
	p3 := part(3, []byte("tail"))
	noop := func(Upload, []Part) (map[string]string, string, error) { return nil, "", nil }
	if _, err := e.CompleteUpload("b", "big", u.ID, []CompletePart{{3, p3.ETag}, {1, p1.ETag}}, Condition{}, noop); !errors.Is(err, ErrInvalidPartOrder) {
		t.Fatalf("order: %v", err)
	}
	if _, err := e.CompleteUpload("b", "big", u.ID, []CompletePart{{3, p3.ETag}, {4, ""}}, Condition{}, noop); !errors.Is(err, ErrInvalidPart) {
		t.Fatalf("missing part: %v", err)
	}
	obj, err := e.CompleteUpload("b", "big", u.ID, []CompletePart{{1, p1.ETag}, {3, p3.ETag}}, Condition{}, noop)
	if err != nil {
		t.Fatal(err)
	}
	if obj.Size != int64(MinPartSize+4) || !strings.HasSuffix(obj.ETag, "-2") {
		t.Fatalf("object %+v", obj)
	}
	if n := blobCount(t, dir); n != 2 {
		t.Fatalf("unused parts not collected: %d blobs", n)
	}
	rc, _ := e.Open(obj, int64(MinPartSize)-2, 5) // a range spanning both parts
	b, _ := io.ReadAll(rc)
	rc.Close()
	if string(b) != "aatai" {
		t.Fatalf("range across parts: %q", b)
	}
	if _, err := e.GetUpload("b", "big", u.ID); !errors.Is(err, ErrNoSuchUpload) {
		t.Fatal("upload should be gone")
	}
	_ = p2
}

func TestReadsSurviveConcurrentReplace(t *testing.T) {
	e := open(t, t.TempDir())
	defer e.Close()
	e.CreateBucket("b")
	put(t, e, "b", "k", strings.Repeat("v0", 1000), Condition{})
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 1; ; i++ {
			select {
			case <-stop:
				return
			default:
				put(t, e, "b", "k", strings.Repeat(fmt.Sprintf("v%d", i%10), 1000), Condition{})
			}
		}
	}()
	for i := 0; i < 300; i++ {
		obj, rc, err := e.GetFresh("b", "k", func(o Object) (int64, int64, error) { return 0, o.Size, nil })
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(rc)
		rc.Close()
		if int64(len(b)) != obj.Size || strings.Count(string(b), string(b[:2])) != 1000 {
			t.Fatalf("torn read: %d bytes", len(b))
		}
	}
	close(stop)
	wg.Wait()
}
