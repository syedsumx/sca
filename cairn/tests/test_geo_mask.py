"""The geo mask and location classification: coordinates rounded, shapes reduced to a rounded centre point, other
values masked as partial; location columns found by name and by value; geo outranks partial, not hash."""

from __future__ import annotations

from cairn.coreproxy.masking import build_plan, mask_scalar
from cairn.dataaccess.trino import mask_expression
from cairn.governance.pii import classify_field


def test_sample_values_are_coarsened_like_trino_does(monkeypatch):
    assert mask_scalar(48.137154, "geo") == 48.14 and mask_scalar("11.576124", "geo") == "11.58"
    assert mask_scalar("POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))", "geo") == "POINT (1.0 1.0)"
    assert mask_scalar("POINT (11.576124 48.137154)", "geo") == "POINT (11.58 48.14)"
    assert mask_scalar("jane@acme.com", "geo") == "j•••@•••.com" and mask_scalar(None, "geo") is None
    monkeypatch.setenv("CAIRN_GEO_MASK_DECIMALS", "1")
    assert mask_scalar(48.137154, "geo") == 48.1 and "round(\"lat\", 1)" in mask_expression("lat", "double", "geo")


def test_trino_expressions_keep_the_type_and_fall_back_to_partial():
    assert mask_expression("lat", "double", "geo") == 'CASE WHEN "lat" IS NULL THEN NULL ELSE CAST(round("lat", 2) AS double) END'
    assert "ST_Centroid" in mask_expression("shape", "geometry", "geo")
    assert mask_expression("n", "integer", "geo") == mask_expression("n", "integer", "partial")
    assert "ST_GeometryFromText" in mask_expression("place", "varchar", "geo")


def test_location_columns_are_found_and_geo_outranks_partial():
    assert [c.kind for c in classify_field("pickup_lat", [])] == ["Location"]
    assert "Location" in [c.kind for c in classify_field("where", ["POINT (1 2)", "POINT (3 4)"])]
    assert "Location" in [c.kind for c in classify_field("pos", ["48.137, 11.575", "52.520, 13.404"])]
    assert "Location" not in [c.kind for c in classify_field("pos", ["123.456, 400.1"])]       # out of range
    resource = {"field_labels": {"lat": ["urn:li:glossaryTerm:Classification.PII.Location"]}}

    def ob(strategy, label="PII"):
        return {"type": "mask_columns", "params": {"labels": [label], "strategy": strategy}}
    assert build_plan([ob("partial"), ob("geo", "PII.Location")], resource).strategy == "geo"
    assert build_plan([ob("geo", "PII.Location"), ob("hash")], resource).strategy == "hash"
