"""A pipe's file version is the same whether the file's time came from a listing (milliseconds, with
pyarrow 26.0.1) or from a single object's HTTP Last-Modified (whole seconds); otherwise a sweep loads
again, and duplicates, every file an event already loaded."""

from types import SimpleNamespace

from cairn.pipelines.pipes import _version


def test_listing_and_head_give_the_same_version():
    listed = SimpleNamespace(size=35, mtime_ns=1_793_705_238_376_000_000)    # 11:27:18.376
    head = SimpleNamespace(size=35, mtime_ns=1_793_705_238_000_000_000)      # 11:27:18
    assert _version(listed) == _version(head) == "35-1793705238000"          # the form already recorded


def test_a_changed_file_is_a_new_version():
    a = SimpleNamespace(size=35, mtime_ns=1_793_705_238_000_000_000)
    assert _version(SimpleNamespace(size=36, mtime_ns=a.mtime_ns)) != _version(a)
    assert _version(SimpleNamespace(size=35, mtime_ns=a.mtime_ns + 1_000_000_000)) != _version(a)
    assert _version(SimpleNamespace(size=0, mtime_ns=None)) == "0-0"
