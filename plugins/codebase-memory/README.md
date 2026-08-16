# codebase-memory

Codebase knowledge graph for agents —
[DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp).
Tree-sitter parsing across 158 languages into a persistent graph of functions,
classes, call chains and routes, queried with 15 MCP tools (structured search,
call-path tracing, git-diff blast radius, read-only Cypher). **Local** stdio
server — baked into the image, runs inside the container, wired into every
installed MCP-capable agent. No secret, no host service, no egress.

```yaml
plugins: [codebase-memory]
```

## Notes

- **Index before querying.** Nothing is automatic: `index_repository` with an
  absolute `repo_path` (`/workspace/repos/<name>`, or a worktree) builds the
  graph; every other tool then takes `project`. Upstream's `auto_index` setting
  is left off on purpose — an agent started in `/workspace/repos` would
  otherwise index *every* repo at once. Measured cost per repo (arm64, 2 GB
  container, first index includes daemon start): this repo 6.8 s / 6 MB, a
  2000-file C codebase 11.0 s / 102 MB. Upstream's "average repo in
  milliseconds" does not describe a cold first index.
- **Not rooted at `$PWD`**, unlike serena and archex. The server is
  project-agnostic and each tool names its own repo, so a single session can
  hold several repos and worktrees at the same time — no `activate_project`
  dance when crossing checkouts.
- **The index persists across container recreates**, via the plugin's declared
  `cbm-cache` volume (see [`plugins/README.md`](../README.md#volumes--state-that-survives-a-recreate)).
  It mounts at the tool's **default** cache path rather than overriding
  `CBM_CACHE_DIR`, because the tool allows exactly one canonical cache root per
  account: a shell-side `codebase-memory-mcp` wouldn't carry the override and
  would be rejected at the admission barrier. `./djinn down <container> --purge`
  removes it; a plain `down` keeps it. Budget for the size — 6–8 MB for a
  typical repo here, but 102 MB for a 2000-file C codebase.
- **A persisted index can be stale where a fresh one can't.** Changes made
  while the container was down aren't watched; the project re-registers with
  the watcher on the next session and catches up by git diff. After a large
  refactor, re-index rather than trusting call edges (`AGENTS.md` tells agents
  the same).
- **~290 MB added to the shared image**, and the plugin loop bakes every
  `plugin.yml`, so that cost lands on *every* container whether or not it
  enables this plugin. It is one statically-linked binary with the grammars,
  embeddings and graph UI all compiled in — there is no slimmer artifact.
- **No runtime network.** The binary never phones home, checks for updates, or
  downloads language servers (its "Hybrid LSP" type resolution is compiled in,
  not a language-server download — the opposite of serena). So no `egress:`
  entry, and it works with the firewall fully closed.
- **A background daemon starts with the first session** (~12 MB RSS) and is
  shared by every agent in the container; it owns the file watcher that keeps
  indexed projects fresh. The last session out shuts it down. It respects
  cgroup CPU/memory limits, so the container's `mem_limit` is honoured without
  setting `CBM_WORKERS` / `CBM_MEM_BUDGET_MB`.
- **Never run `codebase-memory-mcp install` / `update` / `uninstall` inside a
  container.** Those rewrite agent MCP config, skills and hooks — files
  `src/wire_plugins.py` owns and re-derives on every `./djinn up`, and which are
  volume mounts here. To move versions, bump `CBM_VERSION` in
  [`plugin.yml`](plugin.yml) and rebuild the image.
- **The graph UI** (upstream's `--ui=true --port=9749`) is not wired. It would
  need a host port and a compose port mapping; the MCP tools cover the
  agent-facing use.

## Verified

Against `v0.10.1` in a 2 GB `ubuntu:24.04` container as a non-root user (what
the image build and runtime actually do): checksum verified, `--version` clean,
MCP `initialize` + `tools/list` returning all 15 tools, and a real
`index_repository` run over this repo — 1095 nodes / 3617 edges, `status:
"indexed"`, no files written into the repo.
