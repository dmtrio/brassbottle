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

## CI Staging

This branch includes `ci-staged/ci.yml` as the proposed GitHub Actions workflow.
It is staged outside `.github/workflows/` so a human with workflow permission can
move it into place.
