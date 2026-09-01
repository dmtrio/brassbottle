# Vendored UI runtime for the admin plane

Single pinned, self-contained ESM file — no build step, no node, no CDN.

| File | Package | Version | Source | Tarball shasum |
|---|---|---|---|---|
| `htm-preact-standalone-3.1.1.module.js` | `htm` (preact standalone bundle: preact + htm + hooks) | 3.1.1 | https://registry.npmjs.org/htm/-/htm-3.1.1.tgz | `49266582be0dc66ed2235d5ea892307cc0c24b78` |

The file is `package/preact/standalone.module.js` from that tarball, byte-for-
byte. License: `LICENSE-htm` (Apache-2.0; bundled preact is MIT). To upgrade:
fetch the new tarball from the registry, verify its shasum against the
registry metadata, replace the file, and update this table plus the version
in `admin_daemon.py`'s vendor route.
