"""Run an SDK connector as an ordinary ingestion recipe, on Cairn's executor or an agent pool::

    source:
      type: cairn.sdk.bridge.SdkSource
      config:
        source: my_package.module:MySource     # the connector, installed where the recipe runs
        env: PROD
        config: {folder: /data/exports, token: "${MY_TOKEN}"}

The executor imports the connector, checks the connection, and sends what it yields.
"""

from __future__ import annotations

from typing import Any, Iterable

from cairn.sdk.source import load, to_mcps


def _connector(config: dict):
    cls = load(config["source"])
    return cls(), cls.config_model.model_validate(config.get("config") or {})


def proposals(config: dict) -> list[dict]:
    """The connector's metadata as proposals (JSON), shared by the bridge and ``cairn source run``."""
    instance, cfg = _connector(config)
    ok, why = instance.test_connection(cfg)
    if not ok:
        raise RuntimeError(f"cannot connect: {why}")
    return to_mcps(instance.platform, instance.datasets(cfg), config.get("env", "PROD"))


def connection(config: dict) -> tuple[bool, str]:
    try:
        instance, cfg = _connector(config)
        return instance.test_connection(cfg)
    except Exception as e:  # noqa: BLE001 - a test connection reports, never raises
        return False, f"{type(e).__name__}: {e}"


try:  # the ingestion framework is only installed where recipes run (executor, agents)
    from datahub.ingestion.api.common import PipelineContext
    from datahub.ingestion.api.source import CapabilityReport, SourceReport, TestableSource, TestConnectionReport
    from datahub.ingestion.api.workunit import MetadataWorkUnit
    from datahub.metadata.schema_classes import GenericAspectClass, MetadataChangeProposalClass
except ImportError:  # pragma: no cover
    SdkSource = None
else:  # pragma: no cover - exercised where recipes run
    class SdkSource(TestableSource):
        def __init__(self, config: dict, ctx: PipelineContext):
            super().__init__(ctx)
            self.config = config
            self.report = SourceReport()

        @classmethod
        def create(cls, config_dict: dict, ctx: PipelineContext) -> "SdkSource":
            return cls(config_dict, ctx)

        @staticmethod
        def test_connection(config_dict: dict) -> TestConnectionReport:
            ok, why = connection(config_dict)
            return TestConnectionReport(basic_connectivity=CapabilityReport(capable=ok, failure_reason=None if ok else why))

        def get_workunits_internal(self) -> Iterable[Any]:
            for i, p in enumerate(proposals(self.config)):
                mcp = MetadataChangeProposalClass(
                    entityType=p["entityType"], entityUrn=p["entityUrn"], changeType=p["changeType"],
                    aspectName=p["aspectName"],
                    aspect=GenericAspectClass(contentType="application/json", value=p["aspect"]["value"].encode()))
                yield MetadataWorkUnit(id=f"cairn-sdk-{i}", mcp_raw=mcp)

        def get_report(self) -> SourceReport:
            return self.report
