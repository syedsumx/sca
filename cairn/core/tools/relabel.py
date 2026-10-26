#!/usr/bin/env python3
"""Relabel user-facing messages compiled into the Cairn Core engine (jar / war, nested jars too).

    python3 core/tools/relabel.py <in.jar|in.war> <out>

In classes, only CONSTANT_Utf8 entries that read as prose (contain a space) are rewritten, plus the
root-account URN, so class, method, field and descriptor names, header names and config keys are
untouched. Seed data (bootstrap MCPs, root policies) is relabeled as text: the root account becomes
urn:li:corpuser:admin and display names / default source names read Cairn. Every other archive
entry is copied byte-for-byte with its original compression (nested jars inside a war stay STORED,
as the Spring Boot loader requires).
"""

from __future__ import annotations

import io
import re
import struct
import sys
import zipfile

# Whole words only: identifiers such as DataHubPolicyKey or acrylDataHub are never touched.
_W = rb"(?<![A-Za-z0-9_.$-])"
_E = rb"(?![A-Za-z0-9_$-])"
RULES = [
    (re.compile(_W + rb"DataHub Cloud" + _E), b"Cairn"),
    (re.compile(_W + rb"(?:Acryl Data|Acryl)" + _E), b"Cairn"),
    (re.compile(_W + rb"(?:DataHub|Datahub)" + _E), b"Cairn"),
]

ROOT_URN = (b"urn:li:corpuser:datahub", b"urn:li:corpuser:admin")

SEED_RULES = [
    (re.compile(rb"urn:li:corpuser:datahub\b"), ROOT_URN[1]),
    # The root account's profile (bootstrap_mcps/root-user.yaml).
    (re.compile(rb"displayName: DataHub\n([ \t]*)title: DataHub"), rb"displayName: Admin\n\1title: Administrator"),
    # The root account's avatar pointed at the engine's repository; the UI default is used instead.
    (re.compile(rb"\n[ \t]*pictureLink:[ \t]*\"https://raw\.githubusercontent\.com/[^\"]*\""), b""),
    (re.compile(rb"\{\{\^ingestion\.name\}\}datahub-"), b"{{^ingestion.name}}cairn-"),
    # Logo paths follow the UI bundle's renamed assets (core/web/package.py).
    (re.compile(rb"(/assets/[A-Za-z0-9_/.-]*?)(?:datahub|acryl)([A-Za-z0-9_.-]*\.(?:svg|png))", re.I),
     rb"\1cairn\2"),
    *RULES,
]
# Identifier-like constants that are user-visible, rewritten only in the named class.
CLASS_CONSTANTS = {
    "controllers/Application.class": {b"datahub-frontend": b"cairn-web"},   # GET /config "application"
}
SEED_FILES = re.compile(r"^(bootstrap_mcps/.*\.ya?ml|boot/.*\.json|BOOT-INF/classes/boot/.*\.json)$")

# constant-pool tag -> fixed payload size (Utf8 = 1 is variable, handled separately)
_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4,
          18: 4, 19: 2, 20: 2}


def _prose(s: bytes) -> bool:
    # Embedded Avro / Pegasus schemas and other JSON documents are data, not messages.
    head = s.lstrip()[:1]
    return b" " in s and head not in (b"{", b"[") and b'"type"' not in s


def relabel_class(data: bytes, name: str = "") -> tuple[bytes, int]:
    if data[:4] != b"\xca\xfe\xba\xbe":
        return data, 0
    count = struct.unpack_from(">H", data, 8)[0]
    out = io.BytesIO()
    out.write(data[:10])
    pos, i, changed = 10, 1, 0
    while i < count:
        tag = data[pos]
        if tag == 1:
            n = struct.unpack_from(">H", data, pos + 1)[0]
            s = data[pos + 3:pos + 3 + n]
            exact = CLASS_CONSTANTS.get(name, {})
            if s in exact:
                s = exact[s]
                changed += 1
            elif s == ROOT_URN[0]:
                s = ROOT_URN[1]
                changed += 1
            elif _prose(s):
                t = s
                for rx, rep in RULES:
                    t = rx.sub(rep, t)
                if t != s:
                    changed += 1
                    s = t
            out.write(b"\x01" + struct.pack(">H", len(s)) + s)
            pos += 3 + n
        else:
            size = _SIZES[tag]
            out.write(data[pos:pos + 1 + size])
            pos += 1 + size
            if tag in (5, 6):
                i += 1  # long/double take two slots
        i += 1
    out.write(data[pos:])
    return out.getvalue(), changed


def relabel_archive(data: bytes) -> tuple[bytes, int]:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            body = src.read(info)
            if info.filename.endswith(".class"):
                body, n = relabel_class(body, info.filename)
                total += n
            elif SEED_FILES.match(info.filename):
                new = body
                for rx, rep in SEED_RULES:
                    new = rx.sub(rep, new)
                total += new != body
                body = new
            elif info.filename.endswith((".jar", ".war")) and body[:2] == b"PK":
                body, n = relabel_archive(body)
                total += n
            dst.writestr(info, body, compress_type=info.compress_type)
    return buf.getvalue(), total


def main() -> int:
    src, dst = sys.argv[1:3]
    with open(src, "rb") as f:
        data, n = relabel_archive(f.read())
    with open(dst, "wb") as f:
        f.write(data)
    print(f"relabel: {n} messages rewritten in {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
