---
agent: agent
description: "Add a quality check to a dataset and wire it into its pipeline"
tools: ['cairn/*']
---

Add a quality check to ${input:dataset:the dataset} that verifies ${input:rule:what must hold}. Read the dataset and its lineage, write the check as code, plan and propose it, and say which pipeline step will run it.
