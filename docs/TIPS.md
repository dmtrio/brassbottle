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

## Shell Aliases and Completion

`djinn` resolves its own location, so it can run from any directory. One alias
onto the dispatcher gives you every subcommand:

```bash
export DJINN_REPO="$HOME/git/brassbottle"   # adjust to your clone
alias djinn="$DJINN_REPO/djinn"
alias cdj='cd "$DJINN_REPO"'
```

Completion resolves bottle names through `src/common.sh`, which keeps
`BOTTLES_PATH` and the compatibility fallbacks in one place — never hardcode
the bottles directory.

For bash:

```bash
_djinn_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]} sub=${COMP_WORDS[1]}
    local dir f names=""
    case $COMP_CWORD in
        1) names="up down service backup allow keys help" ;;
        2)
            case $sub in
                up|down|allow|keys)
                    dir=$(bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$BOTTLES_PATH"')
                    for f in "$dir"/*.yml; do
                        f=${f##*/}
                        [ "$f" = TEMPLATE.yml ] && continue
                        names="$names ${f%.yml}"
                    done ;;
                service)
                    for f in "$DJINN_REPO"/plugins/*/run.sh; do
                        [ -e "$f" ] || continue
                        f=${f%/run.sh}; names="$names ${f##*/}"
                    done ;;
                backup) names="start stop status logs snapshots check restore browser" ;;
            esac ;;
        3)
            [ "$sub" = backup ] && [ "${COMP_WORDS[2]}" = browser ] \
                && names="start stop status logs url" ;;
    esac
    COMPREPLY=($(compgen -W "$names" -- "$cur"))
}
complete -F _djinn_complete djinn
```

For zsh:

```zsh
_djinn_bottles() {
    local dir
    dir=$(bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$BOTTLES_PATH"')
    local -a names=(${dir}/*.yml(N:t:r))
    compadd -a -- ${names:#TEMPLATE}
}

_djinn() {
    if (( CURRENT == 2 )); then
        compadd up down service backup allow keys help
    elif (( CURRENT == 3 )); then
        case "$words[2]" in
            up|down|allow|keys) _djinn_bottles ;;
            service) compadd -- ${DJINN_REPO}/plugins/*/run.sh(N:h:t) ;;
            backup)  compadd start stop status logs snapshots check restore browser ;;
        esac
    elif (( CURRENT == 4 )) && [[ "$words[2]" == backup && "$words[3]" == browser ]]; then
        compadd start stop status logs url
    fi
}
compdef _djinn djinn
```

If you prefer per-verb aliases (`djup`, `djdown`, …), point them at
`"$DJINN_REPO/djinn up"` and friends and add `complete -F _djinn_complete` /
`compdef` entries for each — but the single `djinn` alias keeps completion in
one place, including `djinn backup`.
