# serena

Semantic code retrieval + editing over LSP ([oraios/serena](https://github.com/oraios/serena)).
**Local** stdio server — baked into the image, runs inside the container, wired
into every installed MCP-capable agent. No secret, no host service.

```yaml
plugins: [serena]
```

## Notes

- **Rooted at `$PWD`**, not a hardcoded path — the server follows the session,
  so it auto-roots at the checkout the agent was started in (a repo under
  `/workspace/repos/`, or a worktree). Start agents inside a checkout; a
  non-Claude agent started elsewhere roots serena there. **The zero-friction
  workflow is one agent per worktree** (launch inside the worktree): serena
  roots there and never needs to switch.
- **Worktrees / switching projects.** Serena's stock `ide-assistant` context
  sets `single_project: true`, which — *when a project is pinned via `--project`*
  — locks serena to that one checkout and **removes** the `activate_project`
  tool (`is_single_project = context.single_project and project is not None`, in
  serena's `agent.py`). So an agent rooted at one repo could neither see nor
  switch to a worktree. We fix that by shipping a derived context,
  **`ide-assistant-worktrees`** (see [`derive_context.py`](derive_context.py)): a
  build-time copy of the upstream context with only `single_project` flipped to
  false. Net effect:
  - `--project "$PWD"` still **auto-roots** at the launch checkout (no
    regression), and
  - `activate_project` **survives**, so one agent can reach across into a
    worktree at runtime: `activate_project /workspace/worktrees/<repo>/<branch>`.

  Serena is single-active-project, so hopping between checkouts means one
  `activate_project` per crossing (sticky until the next switch). The derived
  context is regenerated on every image build, so it tracks whatever serena
  ships; if serena ever renames the flag, the build fails loudly rather than
  silently re-locking worktrees.
- **Language servers download lazily** on first use per language. The base
  allowlist covers Python/TS/JS (github / npm / pythonhosted); this plugin adds
  `blob.core.windows.net` for the Azure-hosted ones (.NET, others). If another
  language's download is firewall-blocked, add the host from the error live:
  ```bash
  ./djinn allow djinn-<name> <host> --save yml
  ```
