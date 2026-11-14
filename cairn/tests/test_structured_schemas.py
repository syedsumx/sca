"""Every structured-output schema Cairn sends is closed: the provider's structured outputs refuse an object whose
properties are open (additionalProperties must be false, on every object), so an open one fails only with a live model."""

import importlib
import pkgutil

import cairn


def _open_objects(schema, path):
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            yield path
        for k, v in schema.items():
            yield from _open_objects(v, f"{path}/{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _open_objects(v, f"{path}[{i}]")


def test_every_output_schema_closes_its_objects():
    found, open_ = 0, []
    for m in pkgutil.walk_packages(cairn.__path__, "cairn."):
        try:
            mod = importlib.import_module(m.name)
        except Exception:  # noqa: BLE001 - optional extras not installed here
            continue
        for name, value in vars(mod).items():
            if name.isupper() and name.endswith("SCHEMA") and isinstance(value, dict) and value.get("type") == "object":
                found += 1
                open_ += list(_open_objects(value, f"{m.name}.{name}"))
    assert found >= 8, found
    assert open_ == [], open_
