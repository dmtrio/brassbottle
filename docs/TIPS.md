# Tips

## Update container memory

The durable way: set `memory:` in `containers/<name>.yml` and rerun
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
