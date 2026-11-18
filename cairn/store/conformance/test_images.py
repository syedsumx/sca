"""Image resizing on the file API: ?width/height/fit on GET /-/files/<bucket>/<key> returns a scaled copy in
the image's own format, authorized like the read it stands for; non-images, ranges and oversized requests are
refused."""

import io
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, READER, REGION

PIL = pytest.importorskip("PIL.Image")


def get(store, path, key=ADMIN, headers=None):
    url = f"{store.endpoint}/-/files/{path}"
    req = AWSRequest(method="GET", url=url, headers=headers or {})
    S3SigV4Auth(Credentials(*key), "s3", REGION).add_auth(req)
    return requests.get(url, headers=dict(req.headers), timeout=30)


def image_bytes(fmt, size=(640, 480)):
    img = PIL.new("RGB", size)
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            img.putpixel((x, y), (x % 256, y % 256, 120))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture(scope="module")
def pics(store):
    s3 = store.client()
    s3.create_bucket(Bucket="pics-resize")
    s3.put_object(Bucket="pics-resize", Key="photo.jpg", Body=image_bytes("JPEG"), ContentType="image/jpeg")
    s3.put_object(Bucket="pics-resize", Key="logo.png", Body=image_bytes("PNG", (300, 300)), ContentType="image/png")
    s3.put_object(Bucket="pics-resize", Key="anim.gif", Body=image_bytes("GIF", (100, 50)), ContentType="image/gif")
    s3.put_object(Bucket="pics-resize", Key="pic.webp", Body=image_bytes("WEBP", (200, 100)), ContentType="image/webp")
    s3.put_object(Bucket="pics-resize", Key="notes.txt", Body=b"plain text", ContentType="text/plain")
    s3.put_object(Bucket="pics-resize", Key="fake.png", Body=b"not really a png", ContentType="image/png")
    return store


def test_resize_modes_and_formats(pics):
    r = get(pics, "pics-resize/photo.jpg?width=160")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = PIL.open(io.BytesIO(r.content))
    assert img.format == "JPEG" and img.size == (160, 120)  # proportions kept
    assert int(r.headers["content-length"]) == len(r.content) and r.headers["etag"].endswith('-160x0"')
    img = PIL.open(io.BytesIO(get(pics, "pics-resize/photo.jpg?width=100&height=100&fit=cover").content))
    assert img.size == (100, 100)
    img = PIL.open(io.BytesIO(get(pics, "pics-resize/photo.jpg?width=100&height=40&fit=fill").content))
    assert img.size == (100, 40)
    img = PIL.open(io.BytesIO(get(pics, "pics-resize/logo.png?height=64").content))
    assert img.format == "PNG" and img.size == (64, 64)
    assert PIL.open(io.BytesIO(get(pics, "pics-resize/anim.gif?width=50").content)).size == (50, 25)
    assert PIL.open(io.BytesIO(get(pics, "pics-resize/pic.webp?width=100").content)).size == (100, 50)
    # Never enlarged: asking for more than the image has returns it at its own size.
    assert PIL.open(io.BytesIO(get(pics, "pics-resize/logo.png?width=1000").content)).size == (300, 300)
    # Without resize parameters the original comes back untouched.
    assert get(pics, "pics-resize/logo.png").content == pics.client().get_object(Bucket="pics-resize", Key="logo.png")["Body"].read()


def test_refusals(pics):
    assert get(pics, "pics-resize/notes.txt?width=10").status_code == 400  # not an image
    assert get(pics, "pics-resize/fake.png?width=10").status_code == 400  # not a readable image
    assert get(pics, "pics-resize/photo.jpg?width=0").status_code == 400
    assert get(pics, "pics-resize/photo.jpg?width=99999").status_code == 400
    assert get(pics, "pics-resize/photo.jpg?width=10&fit=cover").status_code == 400  # cover needs both sides
    assert get(pics, "pics-resize/photo.jpg?fit=cover").status_code == 400
    assert get(pics, "pics-resize/photo.jpg?width=10", headers={"range": "bytes=0-10"}).status_code == 400
    assert get(pics, "pics-resize/missing.jpg?width=10").status_code == 404
    # Authorized as the read it stands for: a key without access to the bucket is refused.
    from conftest import SCOPED

    assert get(pics, "pics-resize/photo.jpg?width=10", key=SCOPED).status_code == 403
    assert get(pics, "pics-resize/photo.jpg?width=10", key=READER).status_code == 200


def test_conditional_requests_use_the_copy_etag(pics):
    """A cache in front revalidates a thumbnail with the thumbnail's own ETag: that answers 304 (without a
    resize), and the original's ETag does not stand for the thumbnail."""
    r = get(pics, "pics-resize/photo.jpg?width=160")
    variant, original = r.headers["etag"], pics.client().head_object(Bucket="pics-resize", Key="photo.jpg")["ETag"]
    assert variant != original
    r = get(pics, "pics-resize/photo.jpg?width=160", headers={"if-none-match": variant})
    assert r.status_code == 304 and r.headers["etag"] == variant
    r = get(pics, "pics-resize/photo.jpg?width=160", headers={"if-none-match": f'"x", W/{variant}'})
    assert r.status_code == 304 and r.headers["etag"] == variant
    # Another size's ETag, or the original's, is not this copy's.
    assert get(pics, "pics-resize/photo.jpg?width=100", headers={"if-none-match": variant}).status_code == 200
    assert get(pics, "pics-resize/photo.jpg?width=160", headers={"if-none-match": original}).status_code == 200
    assert get(pics, "pics-resize/photo.jpg?width=160", headers={"if-match": variant}).status_code == 200
    assert get(pics, "pics-resize/photo.jpg?width=160", headers={"if-match": original}).status_code == 412
    assert get(pics, "pics-resize/photo.jpg?width=160", headers={"if-none-match": "*"}).status_code == 304
    # The plain read keeps the original's validators.
    assert get(pics, "pics-resize/photo.jpg", headers={"if-none-match": original}).status_code == 304


def test_cover_and_fill_never_enlarge(custom_store):
    """A thin image covering (or filled into) a big box is not scaled up: a 2000 x 1 PNG of a few hundred bytes
    covering 4096 x 4096 would otherwise need an 8-million-pixel-wide intermediate (about 100 GB) and take the
    whole store down with it."""
    st = custom_store({})
    s3 = st.client()
    s3.create_bucket(Bucket="pics-thin")
    s3.put_object(Bucket="pics-thin", Key="thin.png", Body=image_bytes("PNG", (2000, 1)), ContentType="image/png")
    s3.put_object(Bucket="pics-thin", Key="dot.png", Body=image_bytes("PNG", (3, 2)), ContentType="image/png")
    r = get(st, "pics-thin/thin.png?width=4096&height=4096&fit=cover")
    assert r.status_code == 200, r.text
    w, h = PIL.open(io.BytesIO(r.content)).size
    assert w == h and w <= 1 and st.proc.poll() is None  # the box's proportions, at the source's own scale
    r = get(st, "pics-thin/thin.png?width=100&height=100&fit=cover")
    assert r.status_code == 200 and PIL.open(io.BytesIO(r.content)).size == (1, 1)
    r = get(st, "pics-thin/thin.png?width=400&height=1&fit=cover")  # smaller: scaled down and cropped as before
    assert r.status_code == 200 and PIL.open(io.BytesIO(r.content)).size == (400, 1)
    r = get(st, "pics-thin/dot.png?width=4096&height=4096&fit=fill")
    assert r.status_code == 200 and PIL.open(io.BytesIO(r.content)).size == (3, 2)
    r = get(st, "pics-thin/dot.png?width=4096&height=1&fit=fill")
    assert r.status_code == 200 and PIL.open(io.BytesIO(r.content)).size == (3, 1)
    assert get(st, "pics-thin/dot.png").status_code == 200 and st.proc.poll() is None


def png16(w, h):
    """A 16-bit RGBA PNG of one colour: a few hundred kilobytes that decode to w x h x 8 bytes."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    z = zlib.compressobj(9)
    row = b"\0" + b"\x12\x34" * (4 * w)
    idat = b"".join(z.compress(row) for _ in range(h)) + z.flush()
    ihdr = struct.pack(">IIBBBBB", w, h, 16, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def peak_mib(pid):
    with open(f"/proc/{pid}/status") as f:
        return next(int(line.split()[1]) // 1024 for line in f if line.startswith("VmHWM"))


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="reads the store's peak memory from /proc")
def test_parallel_resizes_are_limited(custom_store):
    """Each resize of a large source holds hundreds of megabytes (the decoded image and the resampler's
    intermediate), so resizes run a few at a time: parallel requests for one small PNG must not multiply the
    store's memory by the number of requests."""
    st = custom_store({"CAIRN_STORE_IMAGE_RESIZES": "1"})
    s3 = st.client()
    s3.create_bucket(Bucket="pics-many")
    s3.put_object(Bucket="pics-many", Key="big.png", Body=png16(3000, 3000), ContentType="image/png")
    assert get(st, "pics-many/big.png?width=2048").status_code == 200
    one = peak_mib(st.proc.pid)
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: get(st, "pics-many/big.png?width=2048").status_code, range(8)))
    assert codes == [200] * 8
    assert peak_mib(st.proc.pid) < one * 2, (one, peak_mib(st.proc.pid))
