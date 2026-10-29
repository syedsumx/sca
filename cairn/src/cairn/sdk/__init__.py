"""The Cairn connector SDK: build a custom source as a small Python class, check it with the test kit,
and run it from the source gallery on Cairn's executor or an in-VPC agent pool.

    from cairn.sdk import Dataset, Field, Source

    class Config(BaseModel):
        folder: str

    class CsvFolder(Source):
        platform = "csv"
        config_model = Config

        def test_connection(self, config):
            return os.path.isdir(config.folder), f"{config.folder} is not a folder"

        def datasets(self, config):
            for path in glob.glob(f"{config.folder}/*.csv"):
                yield Dataset(name=os.path.basename(path)[:-4], fields=[Field(path=c) for c in header(path)])

See ``cairn.sdk.testkit`` for the conformance checks and ``examples/connectors`` for a full connector.
"""

from cairn.sdk.source import Dataset, Field, Lineage, Source, to_mcps

__all__ = ["Dataset", "Field", "Lineage", "Source", "to_mcps"]
