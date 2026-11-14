"""The upgrade matrix's inputs: pins.json says what each component is tested at, and every file that
pins it says the same; the resolver picks the newest release (no pre-releases) from Docker Hub, PyPI and
Maven Central."""

import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "deploy", "versions"))

import latest  # noqa: E402

PINS = json.load(open(os.path.join(ROOT, "deploy", "versions", "pins.json")))


def test_every_file_pins_what_pins_json_says():
    for name, p in PINS.items():
        if name.startswith("_"):
            continue
        for f in p["files"]:
            text = open(os.path.join(ROOT, f)).read()
            assert p["pinned"] in text, f"{f} does not pin {name} {p['pinned']}"


def test_the_newest_release_is_found():
    pages = {
        "https://hub.docker.com/v2/repositories/trinodb/trino/tags?page_size=100&ordering=last_updated":
            {"results": [{"name": "480"}, {"name": "latest"}, {"name": "479-arm64"}], "next": "p2"},
        "p2": {"results": [{"name": "476"}, {"name": "481"}], "next": None},
        "https://pypi.org/pypi/dbt-core/json": {"releases": {"1.12.6": [{}], "1.13.0b1": [{}], "1.12.7": [{"yanked": True}],
                                                             "1.12.10": [{}], "2.0.0": []}},
    }

    def get(url):
        if url.startswith("https://repo1.maven.org"):
            return b"<metadata><versioning><versions><version>1.10.0</version><version>1.12.0</version>" \
                   b"<version>1.13.0-rc1</version></versions></versioning></metadata>"
        return json.dumps(pages[url]).encode()

    assert latest.latest({"docker": "trinodb/trino", "tags": r"^(\d+)$"}, get) == "481"
    assert latest.latest({"pypi": "dbt-core"}, get) == "1.12.10"        # not the beta, the yanked or the empty one
    assert latest.latest({"maven": "org.apache.iceberg/iceberg-spark-runtime-4.0_2.13"}, get) == "1.12.0"
    rows = latest.report({"trino": {"pinned": "476", "source": {"docker": "trinodb/trino", "tags": r"^(\d+)$"}},
                          "dbt-core": {"pinned": "1.12.10", "source": {"pypi": "dbt-core"}},
                          "gone": {"pinned": "1", "source": {"pypi": "missing"}}}, get)
    assert [(r["component"], r["newer"]) for r in rows] == [("trino", True), ("dbt-core", False), ("gone", False)]
    assert rows[2]["error"]                                             # a source that fails is reported, not hidden
