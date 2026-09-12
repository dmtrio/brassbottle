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

PR 2 adds the `setup:` line that runs
`pi install /opt/plugins/tinfoil/pkg/package` at `up`, offline.

## Security posture

The provider verifies the hardware-attested enclave, encrypts requests to its
attested key, and fails closed. `/tinfoil` re-verifies the enclave. A bare
OpenAI-compatible base URL to `inference.tinfoil.sh` would not be verified and
is deliberately not offered.

See the [Tinfoil documentation](https://docs.tinfoil.sh) and the
[pi provider](https://github.com/tinfoilsh/pi-provider).
