## Codebase Memory (knowledge-graph code search)

- **Index first, once per repo.** `index_repository {"repo_path":
  "/workspace/repos/<name>"}` — absolute path, a worktree path works too. Every
  other tool then refers to the repo by `project`. Nothing is indexed
  automatically, but the index does survive container rebuilds. If a query says
  the project is unknown, index it rather than concluding the code isn't there
  — it takes seconds.
- **Reach for it when the question is structural** — "who calls this", "what
  breaks if I change this", "where are the routes", "what's the shape of this
  service". `get_graph_schema` first (what labels/edges this project actually
  has), then `search_graph` to find symbols, `trace_path` for callers/callees,
  `get_code_snippet` for exact source by qualified name, `query_graph` for
  read-only Cypher when the shape is unusual. `get_architecture` is the
  cheapest way into an unfamiliar repo; `detect_changes` maps your uncommitted
  diff to affected symbols and blast radius before you touch anything risky.
- **Before any negative or exhaustive claim** ("nothing calls this", "this is
  dead code", "that's the only handler"), run `check_index_coverage` on the
  paths you relied on. A clean result means "no recorded gap", not "proof of
  completeness" — files can be excluded by `.gitignore`/`.cbmignore`. Say which
  one you have.
- **The graph can be stale.** A background watcher re-syncs indexed projects,
  but after a large refactor — especially your own — re-run
  `index_repository` before trusting call edges. It answers confidently either
  way, so the check is on you.
- **Never run `codebase-memory-mcp install`, `update`, or `uninstall`.** They
  rewrite MCP config, skills and hooks that this container derives from
  `plugins/`; the version is pinned at image build.
