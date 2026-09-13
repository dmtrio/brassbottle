## Tinfoil pi provider

Use the `tinfoil` pi provider for private work where prompts and files must not
be readable by the inference operator. This applies only in a container that
has the pi agent; without it the plugin is inert and these commands do not
exist.

- Run `pi --provider tinfoil --model <id>`, or choose it through `/model`.
- Models are discovered live from the enclave; `pi --list-models tinfoil` shows
  the available catalogue.
- The footer says `Tinfoil verified` or `Tinfoil unverified`.
- `/tinfoil` re-runs verification and prints the verification document.
- The extension fails closed. `Tinfoil: refusing to send` means enclave
  verification failed, almost always because an egress host is blocked, not
  because of a model problem: check the container egress log and name the host.
- `TINFOIL_API_KEY` comes from the agent shim environment; a 401 means this
  agent's slot is not bound.
- Never point an OpenAI-compatible base URL straight at
  `inference.tinfoil.sh`; that path is unverified.
