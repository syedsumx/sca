"""Image resizing on the file API: ?width/height/fit on GET /-/files/<bucket>/<key> returns a scaled copy in
the image's own format, authorized like the read it stands for; non-images, ranges and oversized requests are
refused."""

import io

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
