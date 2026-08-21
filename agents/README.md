# Agents

> **Ownership rule:** Everything that knows about one agent lives in
> `agents/<name>/`. Nothing in `tests/` names a specific agent (fixture manifests
> excepted, see system goldens). An agent dir never references another agent.

An **agent** is one directory — `agents/<name>/` — describing one CLI that can
be installed into the shared image and (optionally) wired for MCP + per-agent
secrets. A manifest opts in by directory name:

```yaml
agents: [claude, kimi]
```

`agents:` matching is exact string equality against `agents/<name>/` — the
directory name is the agent name. (a manifest still using tools: fails with a pointed rename error)

**What belongs here — and what doesn't.** An agent is an AI CLI a human talks
to, with its own identity: its own shim, its own key file, optionally its own
state volume and rules file. The system's split is *agents use plugins*:
`agents/` holds the things you talk to, `plugins/` holds the MCP capabilities
they use. The schema only *requires* `binary` + `install` (aider is the
minimal case — an AI CLI with no MCP wiring), but that looseness is not an
invitation: a plain binary with no identity of its own belongs in the image's
package layers or a plugin's `install:` block, not here — otherwise this
directory quietly becomes a second package manager.

```
agents/<name>/
  agent.yml      required — descriptor consumed by derive + wiring
  README.md      optional — human docs for this agent
  test_wiring.py optional — wiring contract (invariants + golden/)
  golden/        captured rendered bytes (see SCENARIO_VERSION below)
```

## Adjacent golden contract

`agents/agent_test_kit.py` defines the canonical `wire()` scenario and
`SCENARIO_VERSION` (currently `1`). That integer is API for every adjacent
golden under `agents/*/golden/`: changing any fixture detail (server names,
urls, slots, plugin spec, scratch layout) requires bumping `SCENARIO_VERSION`
and regenerating every `agents/*/golden/` in the same commit
(`GOLDEN_REGEN=1` through the suite or a direct test run).

Capture writes `golden/scenario` (single line, the integer). Verification reads
the stamp first; mismatch or a missing stamp fails with a regen hint.

## Shipped agents

The registry is `agents/*/agent.yml` (not this README table); list them with
`for f in agents/*/agent.yml; do printf '%s -> %s\n' "$(basename "$(dirname "$f")")" "$(yq -r '.binary' "$f")"; done`.

## `agent.yml` schema

Top-level keys are closed to this set:

| Key | Meaning |
|---|---|
| `binary: <cmd>` | Bare executable name (also keys shim generation and MCP payload routing). |
| `install: \|` | Bash baked at image build to install this CLI. |
| `state_dirs: [{path, volume}, ...]` | Home-relative persistent state/auth directories (named volumes). Optional. |
| `rules_file: <home-relative path>` | Global rules file path for this CLI (for composed rules). Optional. |
| `egress: [host, ...]` | Extra firewall allowlist hostnames folded in when enabled. Optional. |
| `mcp: {...}` | MCP descriptor; omit for a non-MCP agent. |

`mcp` keys are closed to:

| Key | Meaning |
|---|---|
| `config_path` | Home-relative config path (`.mcp.json` is only valid with `claude_preapprove`). |
| `format` | `json` or `toml`. |
| `dialect` | `url` \| `httpUrl` \| `type-http` \| `mcpServers` (required for non-strategy JSON wiring). |
| `env_refs` | Boolean or non-empty string; truthy means ref-style, false means literal-key style. |
| `strategy` | `claude_preapprove` \| `codex_managed_block`, or absent for generic JSON path. |

## Closed MCP combo rules

Only these combinations are accepted by `src/manifest.py` (kept in lockstep
with `src/wire_plugins.py` dispatch):

| Descriptor shape | Required constraints |
|---|---|
| `strategy: claude_preapprove` | `config_path: .mcp.json`, `format: json`, truthy `env_refs`. |
| `strategy: codex_managed_block` | `format: toml` (dialect not used by this strategy). |
| Non-strategy literal dialect | `format: json`, `dialect` in `url/httpUrl/type-http`, `env_refs: false`. |
| Non-strategy generic (`mcpServers`) | `format: json`, `dialect: mcpServers`, truthy `env_refs`. |
| Non-strategy TOML | Not allowed (a TOML agent must use a named strategy). |

## How enabling works

- `djinn up` globs `agents/*/agent.yml` and feeds all descriptors into
  `src/manifest.py --derive`.
- `agents:` in the manifest is matched
  **exactly** to agent directory names; matched entries become `AGENTS_ENABLED`.
- `SHIM_AGENTS` and `AGENTS_MCP_JSON` are derived from enabled descriptors with
  an `mcp` block.
- `Dockerfile` and `up.sh` do not carry a hardcoded list of agents; the
  descriptor directory is the registry.

## Adding an agent

1. Create `agents/<name>/agent.yml` with the schema and closed MCP rules above.
2. Add `state_dirs` for any auth/state that must survive recreate.
3. Set `rules_file` if the CLI consumes a global rules document.
4. If MCP-capable, choose the correct descriptor role (strategy, literal, or
   generic `mcpServers`) and validate the combo.
5. Add `agents/<name>/README.md` with install/layout/source notes.
6. Ship `agents/<name>/test_wiring.py` + `golden/` (captured via
   `GOLDEN_REGEN=1`; see any existing agent and `agents/agent_test_kit.py`) —
   invariants hand-written, rendered shapes pinned in bytes beside the
   descriptor. Adjacent tests passing is the install contract.
7. Run the suite when changing descriptors; bump `SCENARIO_VERSION` and regen all
   adjacent goldens when the canonical scenario changes.
8. No `up.sh` / `Dockerfile` / `src` edits — loaders glob this directory; `kimi`
   is the reference example.
