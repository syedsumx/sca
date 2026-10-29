# csv-folder: an example Cairn connector

Every CSV file in a folder becomes a dataset, with its header as the schema.

    pip install -e .                       # or put src/ on the executor's PYTHONPATH
    cairn source check .                   # the conformance test kit
    cairn source run . --config my.yml --dry-run

To offer it in **Settings → Sources**, copy this folder (or just `cairn-source.yaml`) into
`config/source-plugins/`, and install the package wherever the source will run: Cairn's executor
image, or the agent image of the pool that runs it.
