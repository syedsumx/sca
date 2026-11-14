# Example plugins

Built from `examples/skills` and `cairn ide init` by `python examples/plugins/build.py` (CI fails when they drift).

- **`claude-code/cairn`**: a Claude Code plugin: Cairn's MCP server (`CAIRN_URL` in the environment; Claude Code
  signs in with OAuth on first use), the four skills, and the data engineer, steward and platform admin as
  subagents. Install it from this folder's marketplace:

  ```
  /plugin marketplace add ./examples/plugins
  /plugin install cairn@cairn-examples
  ```

- **`copilot`**: the files to copy into a repository for GitHub Copilot: `.vscode/mcp.json`, the coding agent's
  `.github/copilot-mcp.json`, custom agents, prompt files, repository instructions and the skills under
  `.github/skills`. Point them at your Cairn with `cairn ide init --url https://<cairn-host>` instead of copying
  when you can.
