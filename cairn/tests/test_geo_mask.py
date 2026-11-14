"""The geo mask and location classification: coordinates rounded, shapes reduced to a rounded centre point, other
values masked as partial; location columns found by name and by value;
geo is the weakest mask, so partial and hash outrank it (it reveals more)."""

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


def test_location_columns_are_found_and_partial_outranks_geo():
    assert [c.kind for c in classify_field("pickup_lat", [])] == ["Location"]
    assert "Location" in [c.kind for c in classify_field("where", ["POINT (1 2)", "POINT (3 4)"])]
    assert "Location" in [c.kind for c in classify_field("pos", ["48.137, 11.575", "52.520, 13.404"])]
    assert "Location" not in [c.kind for c in classify_field("pos", ["123.456, 400.1"])]       # out of range
    resource = {"field_labels": {"lat": ["urn:li:glossaryTerm:Classification.PII.Location"]}}

    def ob(strategy, label="PII"):
        return {"type": "mask_columns", "params": {"labels": [label], "strategy": strategy}}
    # Partial hides more than geo (51.5074 → "5•••••4" against 51.51): with both, partial wins (review finding).
    assert mask_scalar(51.5074, "partial") == "5•••••4" and mask_scalar(51.5074, "geo") == 51.51
    assert build_plan([ob("partial"), ob("geo", "PII.Location")], resource).strategy == "partial"
    assert build_plan([ob("geo", "PII.Location"), ob("partial")], resource).strategy == "partial"
    assert build_plan([ob("geo", "PII.Location")], resource).strategy == "geo"
    assert build_plan([ob("geo", "PII.Location"), ob("hash")], resource).strategy == "hash"


def test_each_column_takes_the_strongest_mask_of_the_policies_that_hit_it():
    """CI finding on the review fix: with one strategy per dataset, a partial mask on an e-mail column took the geo
    mask away from the location columns of the same table. Each column now takes the strongest mask among the
    policies that hit that column."""
    loc, mail = "urn:li:glossaryTerm:Classification.PII.Location", "urn:li:glossaryTerm:Classification.PII.Email"
    resource = {"field_labels": {"lat": [loc], "place": [loc], "email": [mail]}}

    def ob(strategy, label):
        return {"type": "mask_columns", "params": {"labels": [label], "strategy": strategy}}
    plan = build_plan([ob("partial", "PII.Email"), ob("geo", "PII.Location")], resource)
    assert plan.strategy_for("lat") == "geo" and plan.strategy_for("place") == "geo"
    assert plan.strategy_for("email") == "partial" and plan.strategy == "partial"
    # A broad PII policy covers the location columns too: there the stronger partial wins.
    plan = build_plan([ob("partial", "PII"), ob("geo", "PII.Location")], resource)
    assert plan.strategy_for("lat") == "partial" and plan.strategy_for("email") == "partial"
    # A mask on the whole dataset (its own labels) covers every column at least that strongly.
    plan = build_plan([ob("geo", "PII.Location"), ob("hash", "Confidential")],
                      {**resource, "tags": ["Confidential"]})
    assert plan.all_fields and plan.strategy_for("lat") == "hash" and plan.strategy_for("other") == "hash"
