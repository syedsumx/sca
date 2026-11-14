"""Map charts' tile server: given to the UI only as an https:// URL with {z}, {x} and {y}; anything else stops the
API at start (and none means map charts draw on a plain grid)."""

import pytest

from cairn.app import _map_tiles


def test_the_tile_server_is_https_with_its_placeholders(monkeypatch):
    monkeypatch.delenv("CAIRN_MAP_TILES_URL", raising=False)
    assert _map_tiles() is None
    monkeypatch.setenv("CAIRN_MAP_TILES_URL", "https://tiles.example.com/{z}/{x}/{y}.png")
    assert _map_tiles() == "https://tiles.example.com/{z}/{x}/{y}.png"
    for bad in ("http://tiles.example.com/{z}/{x}/{y}.png", "https://tiles.example.com/{z}/{x}.png",
                "javascript:alert(1)//{z}{x}{y}", 'https://t.example.com/{z}/{x}/{y}.png" onload="x'):
        monkeypatch.setenv("CAIRN_MAP_TILES_URL", bad)
        with pytest.raises(ValueError):
            _map_tiles()
