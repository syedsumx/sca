#!/usr/bin/env python3
"""Replace entries of a jar with the same-named entries of another jar.

    python3 core/tools/overlay_jar.py <base.jar> <overlay.jar> <out.jar>

The web server's own assets jar carries a manifest and favicon that shadow the UI bundle's;
overlaying the Cairn UI jar onto it makes both serve the same files.
"""

import re
import sys
import zipfile

# Images named after the engine that the UI no longer references (it uses renamed copies).
STALE = re.compile(r"^public/.*(datahub|acryl)[^/]*\.(svg|png|jpe?g|ico)$", re.I)

base, overlay, out = sys.argv[1:4]
with zipfile.ZipFile(overlay) as o:
    over = {n: o.read(n) for n in o.namelist()}
replaced = 0
with zipfile.ZipFile(base) as b, zipfile.ZipFile(out, "w") as dst:
    for info in b.infolist():
        if STALE.match(info.filename):
            continue
        body = b.read(info)
        if info.filename in over and not info.filename.endswith("/"):
            body = over[info.filename]
            replaced += 1
        dst.writestr(info, body, compress_type=info.compress_type)
print(f"overlay: {replaced} entries replaced in {base}")
