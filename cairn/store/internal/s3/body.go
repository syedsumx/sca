package s3

import (
	"bufio"
	"bytes"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"hash/crc32"
	"hash/crc64"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
)

// Checksum algorithms S3 clients send (x-amz-checksum-*).
var checksumAlgos = []string{"crc32", "crc32c", "crc64nvme", "sha1", "sha256"}

var crc64NVME = crc64.MakeTable(0x9a6c9329ac4bc9b5)

func newChecksum(algo string) hash.Hash {
	switch algo {
	case "crc32":
		return crc32.NewIEEE()
	case "crc32c":
		return crc32.New(crc32.MakeTable(crc32.Castagnoli))
	case "crc64nvme":
		return crc64.New(crc64NVME)
	case "sha1":
		return sha1.New()
	case "sha256":
		return sha256.New()
	}
	return nil
}

func validAlgo(a string) bool {
	for _, x := range checksumAlgos {
		if x == a {
			return true
		}
	}
	return false
}

var (
	errBadDigest        = errors.New("bad digest")
	errContentSHA       = errors.New("content sha256 mismatch")
	errChunkSignature   = errors.New("chunk signature mismatch")
	errChunkFormat      = errors.New("malformed chunked encoding")
	errIncompleteBody   = errors.New("incomplete body")
	errChecksumMismatch = errors.New("checksum mismatch")
)

// body wraps a request body: it decodes aws-chunked framing, verifies chunk and trailer signatures,
// the declared SHA-256 and any declared checksums, and reports the first failure at EOF.
type body struct {
	r         io.Reader
	declared  map[string]string    // algo -> expected base64 (header or trailer)
	sums      map[string]hash.Hash // algo -> running hash
	sha       hash.Hash
	wantSHA   string
	chunked   *chunkedReader
	wantLen   int64
	n         int64
	finalized bool
	err       error
}

// trailerAlgos are the checksums a request promises in its trailer.
func trailerAlgos(r *http.Request) []string {
	var out []string
	for _, t := range strings.Split(r.Header.Get("X-Amz-Trailer"), ",") {
		t = strings.ToLower(strings.TrimSpace(t))
		if a := strings.TrimPrefix(t, "x-amz-checksum-"); a != t && validAlgo(a) {
			out = append(out, a)
		}
	}
	return out
}

// newBody prepares the reader for a request whose signature was checked.
func newBody(r *http.Request, auth *authResult) (*body, error) {
	b := &body{declared: map[string]string{}, sums: map[string]hash.Hash{}, wantLen: -1}
	for _, a := range checksumAlgos {
		if v := r.Header.Get("X-Amz-Checksum-" + a); v != "" {
			b.declared[a] = v
		}
	}
	for _, a := range trailerAlgos(r) {
		b.sums[a] = newChecksum(a)
	}
	if a := strings.ToLower(r.Header.Get("X-Amz-Sdk-Checksum-Algorithm")); validAlgo(a) {
		b.sums[a] = newChecksum(a)
	}
	for a := range b.declared {
		b.sums[a] = newChecksum(a)
	}
	switch auth.payloadHash {
	case streamingSigned, streamingSignedT, streamingUnsignT:
		dl := r.Header.Get("X-Amz-Decoded-Content-Length")
		n, err := strconv.ParseInt(dl, 10, 64)
		if err != nil || n < 0 {
			return nil, errMissingDecodedLength
		}
		b.wantLen = n
		cr := &chunkedReader{br: bufio.NewReaderSize(r.Body, 64<<10), signed: auth.payloadHash != streamingUnsignT,
			auth: auth, prevSig: auth.seedSig, hasTrailer: auth.payloadHash != streamingSigned}
		b.chunked = cr
		b.r = cr
	case unsignedPayload:
		b.r = r.Body
		b.wantLen = r.ContentLength
	default:
		if len(auth.payloadHash) != 64 {
			return nil, errContentSHAInvalid
		}
		b.r = r.Body
		b.sha = sha256.New()
		b.wantSHA = auth.payloadHash
		b.wantLen = r.ContentLength
	}
	if md := r.Header.Get("Content-Md5"); md != "" {
		if _, err := base64.StdEncoding.DecodeString(md); err != nil {
			return nil, errInvalidDigestErr
		}
	}
	return b, nil
}

func (b *body) Read(p []byte) (int, error) {
	if b.err != nil {
		return 0, b.err
	}
	n, err := b.r.Read(p)
	if n > 0 {
		b.n += int64(n)
		if b.sha != nil {
			b.sha.Write(p[:n])
		}
		for _, h := range b.sums {
			h.Write(p[:n])
		}
	}
	if err == io.EOF {
		if ferr := b.finish(); ferr != nil {
			b.err = ferr
			return n, ferr
		}
	} else if err != nil {
		b.err = err
	}
	return n, err
}

func (b *body) finish() error {
	if b.finalized {
		return nil
	}
	b.finalized = true
	if b.wantLen >= 0 && b.n != b.wantLen {
		return errIncompleteBody
	}
	if b.sha != nil && hex.EncodeToString(b.sha.Sum(nil)) != b.wantSHA {
		return errContentSHA
	}
	if b.chunked != nil {
		for k, v := range b.chunked.trailers {
			if a := strings.TrimPrefix(k, "x-amz-checksum-"); a != k && validAlgo(a) {
				b.declared[a] = v
			}
		}
	}
	for a, want := range b.declared {
		h := b.sums[a]
		if h == nil {
			continue
		}
		if base64.StdEncoding.EncodeToString(h.Sum(nil)) != want {
			return fmt.Errorf("%w: %s", errChecksumMismatch, a)
		}
	}
	return nil
}

// Checksums returns the computed checksums (base64) of the algorithms the client used.
func (b *body) Checksums() map[string]string {
	out := map[string]string{}
	for a, h := range b.sums {
		out[a] = base64.StdEncoding.EncodeToString(h.Sum(nil))
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// chunkedReader decodes aws-chunked bodies:
//
//	<hex-size>[;chunk-signature=<sig>]\r\n<data>\r\n ... 0[;chunk-signature=<sig>]\r\n[trailers\r\n]\r\n
type chunkedReader struct {
	br         *bufio.Reader
	signed     bool
	hasTrailer bool
	auth       *authResult
	prevSig    string
	left       int64
	sigWant    string
	chunk      hash.Hash
	trailers   map[string]string
	done       bool
}

func (c *chunkedReader) readLine() (string, error) {
	line, err := c.br.ReadString('\n')
	if err != nil {
		if err == io.EOF && line != "" {
			return strings.TrimRight(line, "\r\n"), nil
		}
		return "", err
	}
	if len(line) > 4096 {
		return "", errChunkFormat
	}
	return strings.TrimRight(line, "\r\n"), nil
}

func (c *chunkedReader) chunkSig(dataHash string) string {
	toSign := strings.Join([]string{"AWS4-HMAC-SHA256-PAYLOAD", c.auth.amzDate, c.auth.scope, c.prevSig, emptySHA256,
		dataHash}, "\n")
	return hex.EncodeToString(hmacSHA256(c.auth.signingKey, toSign))
}

func (c *chunkedReader) endChunk() error {
	if c.signed {
		got := c.chunkSig(hex.EncodeToString(c.chunk.Sum(nil)))
		if got != c.sigWant {
			return errChunkSignature
		}
		c.prevSig = got
	}
	return nil
}

func (c *chunkedReader) Read(p []byte) (int, error) {
	for {
		if c.done {
			return 0, io.EOF
		}
		if c.left > 0 {
			if int64(len(p)) > c.left {
				p = p[:c.left]
			}
			n, err := c.br.Read(p)
			c.left -= int64(n)
			if c.chunk != nil {
				c.chunk.Write(p[:n])
			}
			if err == io.EOF {
				return n, errIncompleteBody
			}
			if c.left == 0 {
				crlf, err := c.readLine()
				if err != nil || crlf != "" {
					return n, errChunkFormat
				}
				if err := c.endChunk(); err != nil {
					return n, err
				}
			}
			return n, err
		}
		header, err := c.readLine()
		if err != nil {
			return 0, errIncompleteBody
		}
		sizePart, ext, _ := strings.Cut(header, ";")
		size, err := strconv.ParseInt(strings.TrimSpace(sizePart), 16, 64)
		if err != nil || size < 0 {
			return 0, errChunkFormat
		}
		c.sigWant = ""
		if c.signed {
			k, v, _ := strings.Cut(ext, "=")
			if strings.TrimSpace(k) != "chunk-signature" || v == "" {
				return 0, errChunkSignature
			}
			c.sigWant = strings.TrimSpace(v)
			c.chunk = sha256.New()
		}
		if size > 0 {
			c.left = size
			continue
		}
		// final chunk
		if c.signed {
			c.chunk = sha256.New()
			if err := c.endChunk(); err != nil {
				return 0, err
			}
		}
		if err := c.readTrailers(); err != nil {
			return 0, err
		}
		c.done = true
		return 0, io.EOF
	}
}

func (c *chunkedReader) readTrailers() error {
	c.trailers = map[string]string{}
	var trailerSig string
	var lines []string
	for {
		line, err := c.readLine()
		if err != nil {
			if err == io.EOF {
				break
			}
			return errChunkFormat
		}
		if line == "" {
			break
		}
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			return errChunkFormat
		}
		k = strings.ToLower(strings.TrimSpace(k))
		v = strings.TrimSpace(v)
		if k == "x-amz-trailer-signature" {
			trailerSig = v
			continue
		}
		c.trailers[k] = v
		lines = append(lines, k+":"+v)
	}
	if c.signed && c.hasTrailer {
		sort.Strings(lines)
		var buf bytes.Buffer
		for _, l := range lines {
			buf.WriteString(l + "\n")
		}
		toSign := strings.Join([]string{"AWS4-HMAC-SHA256-TRAILER", c.auth.amzDate, c.auth.scope, c.prevSig,
			sha256Hex(buf.Bytes())}, "\n")
		if hex.EncodeToString(hmacSHA256(c.auth.signingKey, toSign)) != trailerSig {
			return errChunkSignature
		}
	}
	return nil
}
