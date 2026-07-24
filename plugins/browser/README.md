# browser

A watchable desktop browser the agent drives (for research containers).
**Remote** HTTP server on the Mac host, port **8814**. Nothing is baked.

```yaml
plugins: [browser]
```

## Start the host service

```bash
./service.sh browser          # default: Brave if installed, else Chrome
./service.sh browser chrome   # extra args are forwarded to the launcher
```

First run self-generates `RESEARCH_BROWSER_KEY` into `secrets.env`; declare it
in `common_secrets` to make it the shared default.

## Notes

- Dedicated instance with its **own** profile dir — none of your cookies,
  sessions, or extensions. Windows appear on your desktop so you can watch and
  interrupt the agent.
- CDP debug port binds localhost only; the bridge requires `X-API-Key`.
