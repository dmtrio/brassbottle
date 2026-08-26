# tests

Host-runnable checks live here. The main entry point is:

```bash
tests/plugins.test.sh
```

It requires `yq`, `jq`, and `python3`.

## What It Covers

`plugins.test.sh` is a broad contract test for the host-side system:

- validates every shipped `plugins/*/plugin.yml` through `src/manifest.py`;
- derives `bottles/TEMPLATE.yml`;
- checks plugin cross-compatibility;
- verifies compose volume/mount contracts;
- runs Python unit tests with `python3 -m unittest discover -s tests`;
- runs host-side bash tests in `tests/bash.test.sh`;
- pins shell-script contracts against the Python modules.

Some checks skip when Docker or Docker Compose are unavailable; the suite prints
those skips explicitly.

## Adjacent Suites

Agent and plugin-specific tests may live beside the descriptor they validate:

- `agents/<name>/test_*.py`
- `plugins/<name>/test_*.py`

The shared discovery tests load those adjacent suites so ownership stays close
to the feature.

## Egress smoke (operator-run, not CI)

`tests/egress.smoke.sh` is the Phase A end-to-end check for the egress broker,
NFLOG reader, and host/container invariants. It needs a **Mac host** with Docker
Desktop, a **running bottle**, and the host broker listening on **8816** (start
it with `./djinn allow --watch` in another tmux window). It cannot run in CI or
inside a container; in those environments it prints `SKIP` and exits zero.

```bash
# With the broker running and a bottle named coding-demo:
./tests/egress.smoke.sh coding-demo

# Or set the bottle name once:
EGRESS_SMOKE_BOTTLE=coding-demo ./tests/egress.smoke.sh
```

Optional overrides:

- `EGRESS_SMOKE_HTTPS_HOST` — blocked HTTPS hostname for the :443 notify check
  (default `docs.stripe.com`)
- `EGRESS_SMOKE_PG_IP` — blocked destination IP for the :5432 NFLOG check
  (default `192.0.2.55`)
- `EGRESS_SMOKE_KILL_BOTTLE` — bottle with `capabilities.egress_broker: false`
  for the live kill-switch iptables / port-grant checks (manifest derive is
  always verified)

The driver logic lives in `tests/egress_smoke_lib.py` (unit-tested by
`tests/test_egress_smoke_lib.py`).

## CI Staging

This branch includes `ci-staged/ci.yml` as the proposed GitHub Actions workflow.
It is staged outside `.github/workflows/` so a human with workflow permission can
move it into place.
