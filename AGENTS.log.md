# AGENTS.log.md — historical notes

History and rationale moved out of the user-facing docs, which state only
current behavior. Newest first.

## 2026-08-19 — bottle / container vocabulary split

Bottles (manifests) and containers used to share the word "container", which
read badly next to the container the manifest produces — so the manifest
sense was renamed to "bottle" (`900dc32`). `CONTAINERS_PATH` and
`$DJINN_HOME/containers` survive as deprecated aliases of `BOTTLES_PATH` /
`$DJINN_HOME/bottles`, each printing a one-line deprecation notice.

## 2026-07-18 — capability flags became plugins

`gateway` / `proxyman` / `browser` were originally `capabilities:` flags;
Plugins v2 Phase 1 (`6130e6c`) migrated them to plugins (`plugins:
[gateway]`). The old flags work for one release with a deprecation warning.
