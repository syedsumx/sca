// Package engine is Cairn Store's storage engine: object bytes in immutable blob files, everything
// else in one transactional index.
//
// Durability and consistency rules:
//
//   - A write streams into a temporary file, is fsync'd, renamed into the blob tree and the directory
//     fsync'd. Only then does a single index transaction make it visible. A reader therefore sees an
//     object only once its bytes are on disk, and sees it immediately after the write returns
//     (read-after-write), because the index commit is durable before the response is sent.
//   - Conditional writes (If-Match / If-None-Match) are checked inside that same transaction, so they
//     are an atomic compare-and-set: two writers cannot both win.
//   - Blobs are never modified. Replacing or deleting an object queues its old blobs for collection
//     in the same transaction; they are removed after the commit. A crash at any point leaves either
//     the old or the new object, never a mix; startup recovery removes temporary files and blobs no
//     object refers to.
package engine

import (
	"crypto/md5"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	ErrNoSuchBucket      = errors.New("no such bucket")
	ErrBucketExists      = errors.New("bucket already exists")
	ErrBucketNotEmpty    = errors.New("bucket not empty")
	ErrNoSuchKey         = errors.New("no such key")
	ErrNoSuchUpload      = errors.New("no such upload")
	ErrPreconditionFails = errors.New("precondition failed")
	ErrInvalidPart       = errors.New("invalid part")
	ErrInvalidPartOrder  = errors.New("invalid part order")
	ErrEntityTooSmall    = errors.New("entity too small")
	ErrConflict          = errors.New("conditional request conflict")
)

const (
	MinPartSize = 5 << 20
	MaxParts    = 10000
)

var (
	bkBuckets = []byte("buckets")
	bkUploads = []byte("uploads")
	bkGC      = []byte("gc")
)

func objBucket(name string) []byte     { return []byte("o:" + name) }
func partsBucket(upload string) []byte { return []byte("p:" + upload) }

// Segment is one stored piece of an object: a whole small object, or one part of a multipart one.
type Segment struct {
	Blob string `json:"b"`
	Size int64  `json:"s"`
}

// Object is the index entry for a key.
type Object struct {
	Key          string            `json:"k"`
	Size         int64             `json:"size"`
	ETag         string            `json:"etag"` // without quotes
	Modified     time.Time         `json:"mtime"`
	ContentType  string            `json:"ct,omitempty"`
	Headers      map[string]string `json:"h,omitempty"`  // content-encoding, cache-control, ...
	UserMeta     map[string]string `json:"um,omitempty"` // x-amz-meta-* (lower-case names, no prefix)
	Checksums    map[string]string `json:"cs,omitempty"` // algorithm -> base64 value (may end in -N)
	ChecksumType string            `json:"cst,omitempty"`
	Segments     []Segment         `json:"seg"`
	PartCount    int               `json:"pc,omitempty"`
}

// Upload is an in-progress multipart upload.
type Upload struct {
	ID           string            `json:"id"`
	Bucket       string            `json:"bucket"`
	Key          string            `json:"key"`
	Initiated    time.Time         `json:"init"`
	ContentType  string            `json:"ct,omitempty"`
	Headers      map[string]string `json:"h,omitempty"`
	UserMeta     map[string]string `json:"um,omitempty"`
	ChecksumAlgo string            `json:"ca,omitempty"`
	ChecksumType string            `json:"cst,omitempty"`
}

// Part is an uploaded part of a multipart upload.
type Part struct {
	Number   int               `json:"n"`
	Blob     string            `json:"b"`
	Size     int64             `json:"s"`
	ETag     string            `json:"etag"`
	MD5      []byte            `json:"md5"`
	Checksum map[string]string `json:"cs,omitempty"`
	Modified time.Time         `json:"mtime"`
}

// BucketInfo describes a bucket.
type BucketInfo struct {
	Name    string    `json:"name"`
	Created time.Time `json:"created"`
}

// Condition is a write precondition evaluated atomically with the commit.
type Condition struct {
	IfMatch     string // ETag the current object must have ("*" = must exist)
	IfNoneMatch string // "*" = must not exist; otherwise the current ETag must differ
}

// Engine is safe for concurrent use.
type Engine struct {
	dir   string
	db    *bolt.DB
	fsync bool
	gcMu  sync.Mutex
}

// Options configure the engine.
type Options struct {
	Fsync bool // fsync blobs, directories and the index (on unless explicitly disabled for tests)
}

// Open opens (or creates) a store in dir and runs crash recovery.
func Open(dir string, opt Options) (*Engine, error) {
	for _, d := range []string{dir, filepath.Join(dir, "blobs"), filepath.Join(dir, "tmp")} {
		if err := os.MkdirAll(d, 0o750); err != nil {
			return nil, err
		}
	}
	db, err := bolt.Open(filepath.Join(dir, "index.db"), 0o600, &bolt.Options{Timeout: 5 * time.Second, NoSync: !opt.Fsync})
	if err != nil {
		return nil, fmt.Errorf("open index: %w", err)
	}
	e := &Engine{dir: dir, db: db, fsync: opt.Fsync}
	if err := db.Update(func(tx *bolt.Tx) error {
		for _, b := range [][]byte{bkBuckets, bkUploads, bkGC} {
			if _, err := tx.CreateBucketIfNotExists(b); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		db.Close()
		return nil, err
	}
	if _, err := e.Recover(); err != nil {
		db.Close()
		return nil, fmt.Errorf("recovery: %w", err)
	}
	return e, nil
}

// Close closes the index.
func (e *Engine) Close() error { return e.db.Close() }

// ---------------------------------------------------------------------------------------------- blobs

func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}

func (e *Engine) blobPath(id string) string {
	return filepath.Join(e.dir, "blobs", id[0:2], id[2:4], id)
}

// BlobWriter receives the bytes of one segment. Commit makes them durable; Abort discards them.
type BlobWriter struct {
	e     *Engine
	id    string
	tmp   *os.File
	n     int64
	md5   hash.Hash
	extra []io.Writer
	done  bool
}

// NewBlob starts a blob; extra writers (checksums) see every byte.
func (e *Engine) NewBlob(extra ...io.Writer) (*BlobWriter, error) {
	id := newID()
	f, err := os.CreateTemp(filepath.Join(e.dir, "tmp"), id+".*")
	if err != nil {
		return nil, err
	}
	return &BlobWriter{e: e, id: id, tmp: f, md5: md5.New(), extra: extra}, nil
}

func (w *BlobWriter) Write(p []byte) (int, error) {
	n, err := w.tmp.Write(p)
	w.n += int64(n)
	w.md5.Write(p[:n])
	for _, x := range w.extra {
		x.Write(p[:n])
	}
	return n, err
}

// Size is the number of bytes written so far.
func (w *BlobWriter) Size() int64 { return w.n }

// MD5 is the digest of the bytes written.
func (w *BlobWriter) MD5() []byte { return w.md5.Sum(nil) }

// Seal fsyncs the blob and moves it into the blob tree; it is not visible to readers until an index
// transaction refers to it.
func (w *BlobWriter) Seal() (Segment, error) {
	if w.done {
		return Segment{}, errors.New("blob already sealed")
	}
	w.done = true
	if w.e.fsync {
		if err := w.tmp.Sync(); err != nil {
			w.tmp.Close()
			os.Remove(w.tmp.Name())
			return Segment{}, err
		}
	}
	if err := w.tmp.Close(); err != nil {
		os.Remove(w.tmp.Name())
		return Segment{}, err
	}
	dst := w.e.blobPath(w.id)
	if err := os.MkdirAll(filepath.Dir(dst), 0o750); err != nil {
		os.Remove(w.tmp.Name())
		return Segment{}, err
	}
	if err := os.Rename(w.tmp.Name(), dst); err != nil {
		os.Remove(w.tmp.Name())
		return Segment{}, err
	}
	if w.e.fsync {
		if err := syncDir(filepath.Dir(dst)); err != nil {
			return Segment{}, err
		}
	}
	return Segment{Blob: w.id, Size: w.n}, nil
}

// Abort discards an unsealed blob.
func (w *BlobWriter) Abort() {
	if !w.done {
		w.done = true
		w.tmp.Close()
		os.Remove(w.tmp.Name())
	}
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

// discard removes sealed blobs that never got committed.
func (e *Engine) discard(segs ...Segment) {
	for _, s := range segs {
		os.Remove(e.blobPath(s.Blob))
	}
}

func queueGC(tx *bolt.Tx, segs []Segment) error {
	b := tx.Bucket(bkGC)
	now := []byte(time.Now().UTC().Format(time.RFC3339))
	for _, s := range segs {
		if err := b.Put([]byte(s.Blob), now); err != nil {
			return err
		}
	}
	return nil
}

// collect deletes blobs queued for garbage collection.
func (e *Engine) collect() {
	e.gcMu.Lock()
	defer e.gcMu.Unlock()
	var ids []string
	_ = e.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bkGC).ForEach(func(k, _ []byte) error {
			ids = append(ids, string(k))
			return nil
		})
	})
	if len(ids) == 0 {
		return
	}
	for _, id := range ids {
		if err := os.Remove(e.blobPath(id)); err != nil && !errors.Is(err, os.ErrNotExist) {
			continue
		}
	}
	_ = e.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bkGC)
		for _, id := range ids {
			if err := b.Delete([]byte(id)); err != nil {
				return err
			}
		}
		return nil
	})
}

// RecoveryReport says what recovery found.
type RecoveryReport struct {
	TempFiles     int `json:"temp_files_removed"`
	Collected     int `json:"blobs_collected"`
	Unreferenced  int `json:"unreferenced_blobs_removed"`
	MissingBlobs  int `json:"objects_with_missing_blobs"`
	ReferencedNow int `json:"blobs_referenced"`
}

// Recover removes temporary files, finishes queued garbage collection and removes blobs no object or
// upload refers to (left by a crash between sealing a blob and committing it). It also counts objects
// whose blobs are missing, which means the disk lost data.
func (e *Engine) Recover() (RecoveryReport, error) {
	var rep RecoveryReport
	tmp := filepath.Join(e.dir, "tmp")
	entries, err := os.ReadDir(tmp)
	if err != nil {
		return rep, err
	}
	for _, ent := range entries {
		if os.Remove(filepath.Join(tmp, ent.Name())) == nil {
			rep.TempFiles++
		}
	}
	var queued int
	_ = e.db.View(func(tx *bolt.Tx) error { queued = tx.Bucket(bkGC).Stats().KeyN; return nil })
	e.collect()
	rep.Collected = queued
	refs := map[string]bool{}
	err = e.db.View(func(tx *bolt.Tx) error {
		return tx.ForEach(func(name []byte, b *bolt.Bucket) error {
			switch {
			case strings.HasPrefix(string(name), "o:"):
				return b.ForEach(func(_, v []byte) error {
					var o Object
					if err := json.Unmarshal(v, &o); err != nil {
						return err
					}
					for _, s := range o.Segments {
						refs[s.Blob] = true
					}
					return nil
				})
			case strings.HasPrefix(string(name), "p:"):
				return b.ForEach(func(_, v []byte) error {
					var p Part
					if err := json.Unmarshal(v, &p); err != nil {
						return err
					}
					refs[p.Blob] = true
					return nil
				})
			}
			return nil
		})
	})
	if err != nil {
		return rep, err
	}
	rep.ReferencedNow = len(refs)
	seen := map[string]bool{}
	root := filepath.Join(e.dir, "blobs")
	err = filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		id := d.Name()
		if refs[id] {
			seen[id] = true
			return nil
		}
		if os.Remove(p) == nil {
			rep.Unreferenced++
		}
		return nil
	})
	if err != nil {
		return rep, err
	}
	for id := range refs {
		if !seen[id] {
			rep.MissingBlobs++
		}
	}
	return rep, nil
}

// ---------------------------------------------------------------------------------------------- buckets

// CreateBucket creates a bucket.
func (e *Engine) CreateBucket(name string) error {
	return e.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(bkBuckets)
		if b.Get([]byte(name)) != nil {
			return ErrBucketExists
		}
		v, _ := json.Marshal(BucketInfo{Name: name, Created: time.Now().UTC()})
		if err := b.Put([]byte(name), v); err != nil {
			return err
		}
		_, err := tx.CreateBucketIfNotExists(objBucket(name))
		return err
	})
}

// DeleteBucket deletes an empty bucket.
func (e *Engine) DeleteBucket(name string) error {
	return e.db.Update(func(tx *bolt.Tx) error {
		if tx.Bucket(bkBuckets).Get([]byte(name)) == nil {
			return ErrNoSuchBucket
		}
		ob := tx.Bucket(objBucket(name))
		if ob != nil {
			if k, _ := ob.Cursor().First(); k != nil {
				return ErrBucketNotEmpty
			}
		}
		var pending bool
		_ = tx.Bucket(bkUploads).ForEach(func(_, v []byte) error {
			var u Upload
			if json.Unmarshal(v, &u) == nil && u.Bucket == name {
				pending = true
			}
			return nil
		})
		if pending {
			return ErrBucketNotEmpty
		}
		if ob != nil {
			if err := tx.DeleteBucket(objBucket(name)); err != nil {
				return err
			}
		}
		return tx.Bucket(bkBuckets).Delete([]byte(name))
	})
}

// HeadBucket reports whether a bucket exists.
func (e *Engine) HeadBucket(name string) (BucketInfo, error) {
	var info BucketInfo
	err := e.db.View(func(tx *bolt.Tx) error {
		v := tx.Bucket(bkBuckets).Get([]byte(name))
		if v == nil {
			return ErrNoSuchBucket
		}
		return json.Unmarshal(v, &info)
	})
	return info, err
}

// ListBuckets lists buckets by name.
func (e *Engine) ListBuckets() ([]BucketInfo, error) {
	var out []BucketInfo
	err := e.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bkBuckets).ForEach(func(_, v []byte) error {
			var b BucketInfo
			if err := json.Unmarshal(v, &b); err != nil {
				return err
			}
			out = append(out, b)
			return nil
		})
	})
	return out, err
}

// ---------------------------------------------------------------------------------------------- objects

func checkCondition(cur *Object, c Condition) error {
	if c.IfNoneMatch != "" {
		if c.IfNoneMatch == "*" {
			if cur != nil {
				return ErrPreconditionFails
			}
		} else if cur != nil && etagMatches(c.IfNoneMatch, cur.ETag) {
			return ErrPreconditionFails
		}
	}
	if c.IfMatch != "" {
		if cur == nil {
			return ErrNoSuchKey
		}
		if c.IfMatch != "*" && !etagMatches(c.IfMatch, cur.ETag) {
			return ErrPreconditionFails
		}
	}
	return nil
}

func etagMatches(header, etag string) bool {
	for _, t := range strings.Split(header, ",") {
		t = strings.TrimSpace(t)
		t = strings.TrimPrefix(t, "W/")
		if t == "*" || strings.Trim(t, `"`) == etag {
			return true
		}
	}
	return false
}

func getObject(tx *bolt.Tx, bucket, key string) (*Object, error) {
	ob := tx.Bucket(objBucket(bucket))
	if ob == nil || tx.Bucket(bkBuckets).Get([]byte(bucket)) == nil {
		return nil, ErrNoSuchBucket
	}
	v := ob.Get([]byte(key))
	if v == nil {
		return nil, nil
	}
	var o Object
	if err := json.Unmarshal(v, &o); err != nil {
		return nil, err
	}
	return &o, nil
}

// PutObject commits obj (whose segments are sealed) under its key, if cond holds. On any error the
// new segments are discarded.
func (e *Engine) PutObject(bucket string, obj Object, cond Condition) error {
	var old []Segment
	err := e.db.Update(func(tx *bolt.Tx) error {
		cur, err := getObject(tx, bucket, obj.Key)
		if err != nil {
			return err
		}
		if err := checkCondition(cur, cond); err != nil {
			return err
		}
		v, err := json.Marshal(obj)
		if err != nil {
			return err
		}
		if err := tx.Bucket(objBucket(bucket)).Put([]byte(obj.Key), v); err != nil {
			return err
		}
		if cur != nil {
			old = cur.Segments
			return queueGC(tx, old)
		}
		return nil
	})
	if err != nil {
		e.discard(obj.Segments...)
		return err
	}
	if len(old) > 0 {
		e.collect()
	}
	return nil
}

// GetObject returns the index entry for a key.
func (e *Engine) GetObject(bucket, key string) (Object, error) {
	var o *Object
	err := e.db.View(func(tx *bolt.Tx) error {
		var err error
		o, err = getObject(tx, bucket, key)
		return err
	})
	if err != nil {
		return Object{}, err
	}
	if o == nil {
		return Object{}, ErrNoSuchKey
	}
	return *o, nil
}

// DeleteObject removes a key; deleting a missing key succeeds (S3 semantics).
func (e *Engine) DeleteObject(bucket, key string, cond Condition) error {
	var old []Segment
	err := e.db.Update(func(tx *bolt.Tx) error {
		cur, err := getObject(tx, bucket, key)
		if err != nil {
			return err
		}
		if cond.IfMatch != "" {
			if err := checkCondition(cur, Condition{IfMatch: cond.IfMatch}); err != nil {
				if errors.Is(err, ErrNoSuchKey) {
					return ErrPreconditionFails
				}
				return err
			}
		}
		if cur == nil {
			return nil
		}
		old = cur.Segments
		if err := tx.Bucket(objBucket(bucket)).Delete([]byte(key)); err != nil {
			return err
		}
		return queueGC(tx, old)
	})
	if err == nil && len(old) > 0 {
		e.collect()
	}
	return err
}

// Open returns a reader over [off, off+n) of an object's bytes, following its segments. The segment
// files are opened up front, so a concurrent replace or delete cannot cut the read short.
func (e *Engine) Open(obj Object, off, n int64) (io.ReadCloser, error) {
	var files []*os.File
	var readers []io.Reader
	closeAll := func() {
		for _, f := range files {
			f.Close()
		}
	}
	pos := int64(0)
	end := off + n
	for _, s := range obj.Segments {
		segStart, segEnd := pos, pos+s.Size
		pos = segEnd
		if segEnd <= off || segStart >= end || s.Size == 0 {
			continue
		}
		f, err := os.Open(e.blobPath(s.Blob))
		if err != nil {
			closeAll()
			return nil, err
		}
		files = append(files, f)
		from := max64(off, segStart) - segStart
		to := min64(end, segEnd) - segStart
		readers = append(readers, io.NewSectionReader(f, from, to-from))
	}
	return &multiCloser{Reader: io.MultiReader(readers...), files: files}, nil
}

type multiCloser struct {
	io.Reader
	files []*os.File
}

func (m *multiCloser) Close() error {
	for _, f := range m.files {
		f.Close()
	}
	return nil
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

// GetFresh looks an object up and opens it, retrying if a concurrent writer replaced it between the
// lookup and the open (its old blobs may already be gone).
func (e *Engine) GetFresh(bucket, key string, rng func(Object) (int64, int64, error)) (Object, io.ReadCloser, error) {
	for attempt := 0; ; attempt++ {
		obj, err := e.GetObject(bucket, key)
		if err != nil {
			return obj, nil, err
		}
		off, n, err := rng(obj)
		if err != nil {
			return obj, nil, err
		}
		rc, err := e.Open(obj, off, n)
		if err == nil {
			return obj, rc, nil
		}
		if !errors.Is(err, os.ErrNotExist) || attempt >= 4 {
			return obj, nil, err
		}
	}
}

// ListOptions select keys to list.
type ListOptions struct {
	Prefix    string
	Delimiter string
	After     string // list keys strictly after this one
	MaxKeys   int
}

// ListResult is one page of a listing.
type ListResult struct {
	Objects        []Object
	CommonPrefixes []string
	Truncated      bool
	NextAfter      string // the last key or common prefix returned
}

// List lists keys in order, grouping by delimiter.
func (e *Engine) List(bucket string, opt ListOptions) (ListResult, error) {
	var res ListResult
	if opt.MaxKeys <= 0 {
		return res, e.db.View(func(tx *bolt.Tx) error {
			if tx.Bucket(bkBuckets).Get([]byte(bucket)) == nil {
				return ErrNoSuchBucket
			}
			return nil
		})
	}
	err := e.db.View(func(tx *bolt.Tx) error {
		if tx.Bucket(bkBuckets).Get([]byte(bucket)) == nil {
			return ErrNoSuchBucket
		}
		ob := tx.Bucket(objBucket(bucket))
		c := ob.Cursor()
		start := opt.Prefix
		if opt.After > start {
			start = opt.After
		}
		k, v := c.Seek([]byte(start))
		count := 0
		for ; k != nil; k, v = c.Next() {
			key := string(k)
			if opt.After != "" && key <= opt.After {
				continue
			}
			if !strings.HasPrefix(key, opt.Prefix) {
				break
			}
			if opt.Delimiter != "" {
				rest := key[len(opt.Prefix):]
				if i := strings.Index(rest, opt.Delimiter); i >= 0 {
					cp := opt.Prefix + rest[:i+len(opt.Delimiter)]
					if opt.After != "" && cp <= opt.After {
						// resuming after this common prefix: skip all of it
						k, v = c.Seek(prefixSuccessor(cp))
						if k == nil {
							break
						}
						k, v = c.Prev()
						continue
					}
					if count >= opt.MaxKeys {
						res.Truncated = true
						return nil
					}
					res.CommonPrefixes = append(res.CommonPrefixes, cp)
					res.NextAfter = cp
					count++
					nk, _ := c.Seek(prefixSuccessor(cp))
					if nk == nil {
						return nil
					}
					k, v = c.Prev()
					continue
				}
			}
			if count >= opt.MaxKeys {
				res.Truncated = true
				return nil
			}
			var o Object
			if err := json.Unmarshal(v, &o); err != nil {
				return err
			}
			res.Objects = append(res.Objects, o)
			res.NextAfter = key
			count++
		}
		return nil
	})
	return res, err
}

// prefixSuccessor is the smallest key greater than every key starting with p.
func prefixSuccessor(p string) []byte {
	b := []byte(p)
	for i := len(b) - 1; i >= 0; i-- {
		if b[i] < 0xff {
			b[i]++
			return b[:i+1]
		}
	}
	return nil
}

// ---------------------------------------------------------------------------------------------- multipart

// CreateUpload starts a multipart upload.
func (e *Engine) CreateUpload(u Upload) (Upload, error) {
	u.ID = newID()
	u.Initiated = time.Now().UTC()
	err := e.db.Update(func(tx *bolt.Tx) error {
		if tx.Bucket(bkBuckets).Get([]byte(u.Bucket)) == nil {
			return ErrNoSuchBucket
		}
		v, _ := json.Marshal(u)
		if err := tx.Bucket(bkUploads).Put([]byte(u.ID), v); err != nil {
			return err
		}
		_, err := tx.CreateBucket(partsBucket(u.ID))
		return err
	})
	return u, err
}

func getUpload(tx *bolt.Tx, bucket, key, id string) (*Upload, error) {
	v := tx.Bucket(bkUploads).Get([]byte(id))
	if v == nil {
		return nil, ErrNoSuchUpload
	}
	var u Upload
	if err := json.Unmarshal(v, &u); err != nil {
		return nil, err
	}
	if u.Bucket != bucket || u.Key != key {
		return nil, ErrNoSuchUpload
	}
	return &u, nil
}

// GetUpload returns an upload.
func (e *Engine) GetUpload(bucket, key, id string) (Upload, error) {
	var u *Upload
	err := e.db.View(func(tx *bolt.Tx) error {
		var err error
		u, err = getUpload(tx, bucket, key, id)
		return err
	})
	if err != nil {
		return Upload{}, err
	}
	return *u, nil
}

func partKey(n int) []byte {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], uint32(n))
	return b[:]
}

// PutPart records a sealed part, replacing any earlier upload of the same part number.
func (e *Engine) PutPart(bucket, key, id string, p Part) error {
	var old []Segment
	err := e.db.Update(func(tx *bolt.Tx) error {
		if _, err := getUpload(tx, bucket, key, id); err != nil {
			return err
		}
		pb := tx.Bucket(partsBucket(id))
		if prev := pb.Get(partKey(p.Number)); prev != nil {
			var op Part
			if json.Unmarshal(prev, &op) == nil {
				old = append(old, Segment{Blob: op.Blob, Size: op.Size})
			}
		}
		v, _ := json.Marshal(p)
		if err := pb.Put(partKey(p.Number), v); err != nil {
			return err
		}
		return queueGC(tx, old)
	})
	if err != nil {
		e.discard(Segment{Blob: p.Blob, Size: p.Size})
		return err
	}
	if len(old) > 0 {
		e.collect()
	}
	return nil
}

// ListParts lists an upload's parts after a part number.
func (e *Engine) ListParts(bucket, key, id string, after, max int) ([]Part, bool, error) {
	var out []Part
	truncated := false
	err := e.db.View(func(tx *bolt.Tx) error {
		if _, err := getUpload(tx, bucket, key, id); err != nil {
			return err
		}
		c := tx.Bucket(partsBucket(id)).Cursor()
		for k, v := c.Seek(partKey(after + 1)); k != nil; k, v = c.Next() {
			if len(out) >= max {
				truncated = true
				return nil
			}
			var p Part
			if err := json.Unmarshal(v, &p); err != nil {
				return err
			}
			out = append(out, p)
		}
		return nil
	})
	return out, truncated, err
}

// ListUploads lists a bucket's in-progress uploads, by key then initiation time.
func (e *Engine) ListUploads(bucket, prefix string) ([]Upload, error) {
	var out []Upload
	err := e.db.View(func(tx *bolt.Tx) error {
		if tx.Bucket(bkBuckets).Get([]byte(bucket)) == nil {
			return ErrNoSuchBucket
		}
		return tx.Bucket(bkUploads).ForEach(func(_, v []byte) error {
			var u Upload
			if err := json.Unmarshal(v, &u); err != nil {
				return err
			}
			if u.Bucket == bucket && strings.HasPrefix(u.Key, prefix) {
				out = append(out, u)
			}
			return nil
		})
	})
	sort.Slice(out, func(i, j int) bool {
		if out[i].Key != out[j].Key {
			return out[i].Key < out[j].Key
		}
		return out[i].Initiated.Before(out[j].Initiated)
	})
	return out, err
}

// AbortUpload discards an upload and its parts.
func (e *Engine) AbortUpload(bucket, key, id string) error {
	err := e.db.Update(func(tx *bolt.Tx) error {
		if _, err := getUpload(tx, bucket, key, id); err != nil {
			return err
		}
		var segs []Segment
		pb := tx.Bucket(partsBucket(id))
		_ = pb.ForEach(func(_, v []byte) error {
			var p Part
			if json.Unmarshal(v, &p) == nil {
				segs = append(segs, Segment{Blob: p.Blob, Size: p.Size})
			}
			return nil
		})
		if err := tx.DeleteBucket(partsBucket(id)); err != nil {
			return err
		}
		if err := tx.Bucket(bkUploads).Delete([]byte(id)); err != nil {
			return err
		}
		return queueGC(tx, segs)
	})
	if err == nil {
		e.collect()
	}
	return err
}

// CompletePart names a part the client wants in the object.
type CompletePart struct {
	Number int
	ETag   string
}

// CompleteUpload assembles the listed parts into the object (no bytes are copied: the object refers
// to the part blobs). finish computes the object's checksums from the chosen parts; it may return an
// error to fail the completion.
func (e *Engine) CompleteUpload(bucket, key, id string, want []CompletePart, cond Condition,
	finish func(u Upload, parts []Part) (map[string]string, string, error)) (Object, error) {
	var obj Object
	var old, unused []Segment
	err := e.db.Update(func(tx *bolt.Tx) error {
		u, err := getUpload(tx, bucket, key, id)
		if err != nil {
			return err
		}
		pb := tx.Bucket(partsBucket(id))
		have := map[int]Part{}
		_ = pb.ForEach(func(_, v []byte) error {
			var p Part
			if json.Unmarshal(v, &p) == nil {
				have[p.Number] = p
			}
			return nil
		})
		if len(want) == 0 {
			return ErrInvalidPart
		}
		var parts []Part
		last := 0
		for _, w := range want {
			if w.Number <= last {
				return ErrInvalidPartOrder
			}
			last = w.Number
		}
		for _, w := range want {
			p, ok := have[w.Number]
			if !ok || (w.ETag != "" && strings.Trim(w.ETag, `"`) != p.ETag) {
				return ErrInvalidPart
			}
			parts = append(parts, p)
		}
		for i, p := range parts {
			if i < len(parts)-1 && p.Size < MinPartSize {
				return ErrEntityTooSmall
			}
		}
		cur, err := getObject(tx, bucket, key)
		if err != nil {
			return err
		}
		if err := checkCondition(cur, cond); err != nil {
			return err
		}
		sum := md5.New()
		var size int64
		var segs []Segment
		chosen := map[int]bool{}
		for _, p := range parts {
			sum.Write(p.MD5)
			size += p.Size
			segs = append(segs, Segment{Blob: p.Blob, Size: p.Size})
			chosen[p.Number] = true
		}
		for n, p := range have {
			if !chosen[n] {
				unused = append(unused, Segment{Blob: p.Blob, Size: p.Size})
			}
		}
		checksums, ctype, err := finish(*u, parts)
		if err != nil {
			return err
		}
		obj = Object{Key: key, Size: size, ETag: fmt.Sprintf("%s-%d", hex.EncodeToString(sum.Sum(nil)), len(parts)),
			Modified: time.Now().UTC(), ContentType: u.ContentType, Headers: u.Headers, UserMeta: u.UserMeta,
			Checksums: checksums, ChecksumType: ctype, Segments: segs, PartCount: len(parts)}
		v, _ := json.Marshal(obj)
		if err := tx.Bucket(objBucket(bucket)).Put([]byte(key), v); err != nil {
			return err
		}
		if cur != nil {
			old = cur.Segments
		}
		if err := tx.DeleteBucket(partsBucket(id)); err != nil {
			return err
		}
		if err := tx.Bucket(bkUploads).Delete([]byte(id)); err != nil {
			return err
		}
		return queueGC(tx, append(append([]Segment{}, old...), unused...))
	})
	if err == nil && len(old)+len(unused) > 0 {
		e.collect()
	}
	return obj, err
}

// Stats summarises the store for metrics.
type Stats struct {
	Buckets int   `json:"buckets"`
	Objects int   `json:"objects"`
	Bytes   int64 `json:"bytes"`
	Uploads int   `json:"uploads"`
}

// Stats counts buckets, objects, stored bytes and open uploads.
func (e *Engine) Stats() (Stats, error) {
	var st Stats
	err := e.db.View(func(tx *bolt.Tx) error {
		st.Buckets = tx.Bucket(bkBuckets).Stats().KeyN
		st.Uploads = tx.Bucket(bkUploads).Stats().KeyN
		return tx.ForEach(func(name []byte, b *bolt.Bucket) error {
			if !strings.HasPrefix(string(name), "o:") {
				return nil
			}
			return b.ForEach(func(_, v []byte) error {
				var o Object
				if json.Unmarshal(v, &o) == nil {
					st.Objects++
					st.Bytes += o.Size
				}
				return nil
			})
		})
	})
	return st, err
}
