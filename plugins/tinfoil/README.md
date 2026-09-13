# tinfoil

An env-only plugin that bakes Tinfoil's official pi provider package into the
image. It is not an MCP server and adds no service.

## Enable it

```yaml
plugins: [tinfoil]
agent_secrets:
  - {agent: pi, slot: TINFOIL_API_KEY, secret: TINFOIL_KEY_pi}
```

Set `TINFOIL_KEY_pi` in the bottle's local `secrets.env`. The plugin needs an
image rebuild because its `install:` block runs at image build time.

`setup:` runs `setup.sh` at every `./djinn up`, which registers the baked
package into pi, offline. Without the pi agent it logs one line and exits 0. The
plugin's [AGENTS.md](AGENTS.md) gives pi agents the provider's usage and
failure guidance.

## Verify inside the container

```bash
pi --list-models tinfoil                 # live catalogue when verified, bundled fallback otherwise
grep -n tinfoil ~/.pi/agent/settings.json
cat /tmp/djinn-setup/tinfoil.log         # last setup run
```

Interactive check: start pi, use `/model` to pick a tinfoil model, and confirm
the footer says `Tinfoil verified`; `/tinfoil` prints the verification document.

## Security posture

The provider verifies the hardware-attested enclave, encrypts requests to its
attested key, and fails closed. `/tinfoil` re-verifies the enclave. A bare
OpenAI-compatible base URL to `inference.tinfoil.sh` would not be verified and
is deliberately not offered. Note the firewall does not enforce that: the
`tinfoil.sh` egress zone lets any process in the bottle reach the enclave
over plain HTTPS, so the guarantee comes from the extension's fail-closed
path, not from the allowlist.

See the [Tinfoil documentation](https://docs.tinfoil.sh) and the
[pi provider](https://github.com/tinfoilsh/pi-provider).
