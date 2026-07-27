## Browser MCP (host-side)

The browser MCP runs on the **host**, not in the container. `filePath` arguments to
`take_screenshot` and `upload_file` are **host** paths — the MCP's path guard
checks them on the Mac.

- Write under the host `TMPDIR` for this container:
  `<BASE_PATH>/browser-tmp/<container>`. That directory is bind-mounted into
  the container at `/artifacts/browser`, so files written there are visible on
  both sides. Nested subdirectories under that path are fine.
- Anything outside that directory is denied by the MCP's path guard. That is
  expected — do not hunt for other writable paths or use
  `--allowUnrestrictedPaths`.
- A relative `filePath` resolves against the host process working directory, not
  the container. Always pass an **absolute host path** under the TMPDIR above.

To find the concrete host path (rather than guessing at `<BASE_PATH>`), read the
bind mount's source out of `/proc/self/mountinfo` — prepend the mount root shown
after `/run/host_mark` to the path in field 4:

```bash
grep -E " /artifacts/browser " /proc/self/mountinfo
# 266 256 0:46 /deme/dev-agent/browser-tmp/<container> /artifacts/browser … /run/host_mark/Users …
#              ^ host path is /Users + this  →  /Users/deme/dev-agent/browser-tmp/<container>
```
