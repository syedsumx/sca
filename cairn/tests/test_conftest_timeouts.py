"""The tests' own requests wait 30 s by default (see conftest.py); Cairn's code and explicit timeouts are untouched."""

import httpx


def test_a_test_request_gets_the_longer_default():
    c = httpx.Client()
    try:
        assert c.timeout.read == 30.0
    finally:
        c.close()
    c = httpx.Client(timeout=2)
    try:
        assert c.timeout.read == 2.0                    # what a test asks for is kept
    finally:
        c.close()


def test_code_outside_the_tests_keeps_httpx_s_default():
    # A call compiled as if it were in another file (Cairn's own code) is left as httpx makes it: 5 s.
    scope = {"httpx": httpx}
    exec(compile("client = httpx.Client()", "/outside/cairn_module.py", "exec"), scope)
    try:
        assert scope["client"].timeout.read == 5.0
    finally:
        scope["client"].close()
