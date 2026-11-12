# Example agents

Cairn custom agents (built in Cairn: instructions, tools and scopes; they run as the person who starts them, narrowed
to their tools and scopes, and every call is traced). Each folder has the agent (`agent.yaml`) and its cases
(`evals.yaml`).

| Agent | What it does | Scopes |
|---|---|---|
| [`quality-triage`](quality-triage/agent.yaml) | Finds why a dataset is unhealthy: its checks, what feeds it, what it breaks downstream. | read |
| [`pii-steward`](pii-steward/agent.yaml) | Finds personal data that is not labelled and proposes tags, descriptions and owners for review. | read, propose |
| [`change-impact-reviewer`](change-impact-reviewer/agent.yaml) | Reviews a proposed change to a dataset (for a pull request): what breaks downstream and whom to tell. | read |
| [`metric-analyst`](metric-analyst/agent.yaml) | Answers business questions with governed metrics first, then governed SQL, showing the definitions used. | read, query_data |
| [`onboarding-guide`](onboarding-guide/agent.yaml) | Helps a newcomer find the data, data products and documentation for their area. | read |
| [`access-concierge`](access-concierge/agent.yaml) | Checks whether someone may use a dataset and, if not, files a well-formed access request. | read, propose |

Install one (YAML in, as JSON):

```bash
python -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' \
  examples/agents/quality-triage/agent.yaml \
  | curl -sf -X POST "$CAIRN/cairn/api/v1/custom-agents" -H "Authorization: Bearer $TOKEN" \
         -H 'content-type: application/json' -d @-
curl -sf -X POST "$CAIRN/cairn/api/v1/custom-agents/quality-triage/deploy" -H "Authorization: Bearer $TOKEN"
curl -sf -X POST "$CAIRN/cairn/api/v1/custom-agents/quality-triage/run" -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"input": "Why is sales.orders failing?"}'
```

or keep them as code (`custom_agents` in a bundle, `cairn_custom_agent` in Terraform). Agents that build pipelines
or change configuration work through the whole API and change requests instead: see the Copilot and Claude Code
agents in `examples/plugins/` and the skills in `examples/skills/`.
