# archex

Verified code-context retrieval for agents ([Mathews-Tom/archex](https://github.com/Mathews-Tom/archex)).
Instead of exploring a repo file-by-file, archex does upfront structural analysis
(BM25F ranking, graph expansion, type packing, token-budgeted assembly) and
returns **ranked code chunks plus proof-bar receipts** that state what was
included, skipped, and whether the bundle is complete enough to act on.

**Local** stdio server — baked into the image, runs inside the container, wired
into every installed MCP-capable agent. Deterministic and local-first: no API
key, no hosted inference, no secret, no host service.

```yaml
plugins: [archex]
```

## Tools & the proof-bar workflow

Prefer these over blind `grep`/file-walking:

- **`query`** — semantic code search, ranked results + receipt.
- **`scout`** — budget-constrained exploration for broad questions.
- **`fetch`** — exact symbol/chunk retrieval by handle.
- **`symbol`** — direct symbol lookup in indexed code.

Every result carries a **proof bar**: freshness, index revision, skipped
candidates, omitted edges, token-budget spend, and a completeness verdict. When a
receipt says it's incomplete (e.g. `incomplete: dependency_frontier_cut`), follow
its recommended next action (`fetch_skipped_candidate`) and re-check before
acting — don't treat a partial bundle as the whole picture.

## Notes

- **Rooted at `$PWD`**, not a hardcoded path — archex follows the session, so it
  indexes/queries the checkout the agent was started in (main, or a worktree).
  Each checkout keeps its own repo-local `.archex/` state (gitignored — see the
  repo `.gitignore`, alongside serena's `.serena/`).
- **First use in a fresh checkout indexes lazily.** The server builds the index
  (`archex init && archex index`) before serving — so the *first* archex tool
  call in a new checkout responds a little late while the index builds. Nothing
  else blocks: the build runs inside the archex MCP subprocess, never your
  terminal, shell, or other tools/MCP servers. The build is gated on a completion
  sentinel written only after `archex index` succeeds, so a failed or interrupted
  first index **self-heals** — the next start rebuilds rather than serving a
  partial index. Force a clean rebuild any time with `rm -rf .archex`.
- **`archex doctor`** for a health check; **`archex query "…"`** to test
  retrieval from the shell.
- **Vectors are opt-in.** The default BM25 retrieval is fully offline. Enabling
  vector embeddings downloads a FastEmbed model from HuggingFace; the plugin
  pre-allows the HuggingFace hosts, but if another host is needed, add it live:
  ```bash
  ./djinn allow djinn-<name> <host> --save yml
  ```
