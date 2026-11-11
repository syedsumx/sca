# PyInstaller: the `cairn` command line as one file (dist/cairn, dist/cairn.exe), from ../src with only the client's
# dependencies (httpx, PyYAML, keyring, pydantic, croniter). Run from cairn/:
#   pyinstaller --clean --noconfirm cli/cairn.spec
# Signing is optional and off by default: on macOS, CAIRN_CODESIGN_IDENTITY (a "Developer ID Application" identity in
# the keychain) signs the binary with the hardened runtime; Windows binaries are signed after the build (signtool).
# -*- mode: python ; coding: utf-8 -*-
import os

src = os.path.abspath(os.path.join(SPECPATH, "..", "src"))  # noqa: F821 - SPECPATH is PyInstaller's

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "cairn_entry.py")],  # noqa: F821
    pathex=[src],
    binaries=[],
    datas=[],
    # keyring finds its backends through entry points; name the ones each OS uses so they are bundled.
    hiddenimports=["keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService",
                   "keyring.backends.kwallet", "keyring.backends.chainer", "keyring.backends.fail",
                   "cairn.flows.cli", "cairn.flows.model", "cairn.flows.expr", "cairn.flows.airflow_dag",
                   "cairn.iac.bundle", "cairn.iac.export", "cairn.iac.kinds", "cairn.governance.abac",
                   "cairn.marketplace.catalog_cli"],
    hookspath=[],
    runtime_hooks=[],
    # The server never goes in: if one of these is imported, the client imported server code (a bug).
    excludes=["fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "sqlglot", "anthropic", "mcp", "trino",
              "psycopg", "pyiceberg", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cairn",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=os.environ.get("CAIRN_TARGET_ARCH") or None,
    codesign_identity=os.environ.get("CAIRN_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
)
