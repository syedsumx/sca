#!/usr/bin/env python3
"""Package the built web UI (vite dist/) as the web application's assets jar.

    python3 core/web/package.py <dist-dir> <brand-dir> <out.jar>

Asset files named after the engine are renamed (images get the Cairn mark), every reference in
the bundle is updated, and the Cairn brand assets are added under assets/brand/. The jar holds
the files under public/, where the web server serves them from.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

UPSTREAM = re.compile(r"datahub|acryl", re.I)
IMAGES = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp")
TEXT = (".js", ".css", ".html", ".json", ".txt", ".webmanifest")
# Served by the web server's own error pages under this exact name.
KEEP = {"datahub-error-pages.css"}
# Product-tour videos and screenshots of the vendor's UI; their panels are disabled by rebrand.mjs.
DROP = re.compile(r"^(FTE-[\w-]+\.mp4|create-source-modal[\w-]*\.jpg)$")


def main() -> int:
    dist, brand, out = (os.path.abspath(p) for p in sys.argv[1:4])
    mark_svg = open(os.path.join(brand, "cairn-mark.svg"), "rb").read()
    mark_png = open(os.path.join(brand, "logo512.png"), "rb").read()

    files: dict[str, bytes] = {}
    for root, _, names in os.walk(dist):
        for n in names:
            p = os.path.join(root, n)
            files[os.path.relpath(p, dist).replace(os.sep, "/")] = open(p, "rb").read()

    dropped = [rel for rel in files if DROP.match(rel.rsplit("/", 1)[-1])]
    for rel in dropped:
        del files[rel]

    renames: dict[str, str] = {}
    for rel in list(files):
        base = rel.rsplit("/", 1)[-1]
        if base in KEEP or not UPSTREAM.search(base):
            continue
        new_base = UPSTREAM.sub("cairn", base)
        new_rel = rel[: len(rel) - len(base)] + new_base
        if new_rel in files or new_rel in renames.values():
            raise SystemExit(f"rename collision: {rel} -> {new_rel}")
        body = files.pop(rel)
        if base.lower().endswith(".svg"):
            body = mark_svg
        elif base.lower().endswith(IMAGES):
            body = mark_png
        files[new_rel] = body
        renames[base] = new_base

    olds = sorted(renames, key=len, reverse=True)
    if olds:
        rx = re.compile("|".join(re.escape(o) for o in olds).encode())
        for rel, body in files.items():
            if rel.endswith(TEXT):
                files[rel] = rx.sub(lambda m: renames[m.group(0).decode()].encode(), body)

    files["assets/brand/cairn-logo.svg"] = mark_svg

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            z.writestr(f"public/{rel}", files[rel])
    print(f"package: {len(files)} files, {len(renames)} renamed, {len(dropped)} dropped -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
