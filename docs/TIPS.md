# Tips

## Update container memory

The durable way: set `memory:` in `bottles/<name>.yml` and rerun
`./djinn up <name>` — the manifest is the source of truth, and anything set
another way is overwritten on the next up.

For a **temporary** bump on a running container without a restart
(reverts on the next `djinn up`):

```bash
# Set a specific limit (containers are named djinn-<name>)
docker update --memory 12g --memory-swap 12g djinn-<name>

# Check current limit (returns bytes, 0 = unlimited)
docker inspect djinn-<name> --format '{{.HostConfig.Memory}}'
```

## Move Runtime State

By default, runtime state lives in the repo's gitignored `./.djinn/` directory.
Use a gitignored `.env` at the repo root to move it:

```bash
DJINN_HOME="$HOME/djinn"
RULES_PATH="$HOME/git/agent-conf/rules"
BOTTLES_PATH="$HOME/git/djinn-bottles"
DJINN_SUBNET="172.30.0.0/24"
```

If `DJINN_HOME` is set and `$DJINN_HOME/rules` or `$DJINN_HOME/bottles` exists,
those directories are auto-detected. Explicit `RULES_PATH` and `BOTTLES_PATH`
still win.

## Persistence Map

| State | Lives in | Survives recreate | Survives `--purge` |
|---|---|---|---|
| Code | workspace volume plus git remotes | yes | no, except in git |
| Agent logins and MCP approvals | per-container auth volumes | yes | no |
| Identity keys | `secrets.env`, composed at `up` | yes | yes |
| Rules | bundled `rules/` or your `RULES_PATH` repo | yes | yes |
| Non-code outputs | `$DJINN_HOME/artifacts/<name>/` mounted at `/artifacts` | yes | yes |

## Shell Aliases

`djinn` resolves its own location, so it can run from any directory. If you want
short commands, add aliases to your shell config:

```bash
export DJINN_REPO="$HOME/git/brassbottle"
alias djup="$DJINN_REPO/djinn up"
alias djdown="$DJINN_REPO/djinn down"
alias djsvc="$DJINN_REPO/djinn service"
alias djallow="$DJINN_REPO/djinn allow"
alias djkeys="$DJINN_REPO/djinn keys"
alias cddj="cd \$DJINN_REPO"
```

Container-name completion can delegate to `src/common.sh`, which keeps
`BOTTLES_PATH` and compatibility resolution in one place:

```bash
_dj_names() {
  local dir f names=""
  dir=$(bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$BOTTLES_PATH"')
  for f in "$dir"/*.yml; do
    f=${f##*/}
    [ "$f" = TEMPLATE.yml ] && continue
    names="$names ${f%.yml}"
  done
  COMPREPLY=($(compgen -W "$names" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _dj_names djup djdown
```

For zsh, use the same aliases and native glob handling:

```zsh
_dj_names_zsh() {
  local dir
  dir=$(bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$BOTTLES_PATH"')
  local -a names=(${dir}/*.yml(N:t:r))
  names=(${names:#TEMPLATE})
  compadd -a names
}
compdef _dj_names_zsh djup djdown
```
