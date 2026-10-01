# Containment probes

A lab and one test per non-host probe of the egress containment epic
(A-root, A-spoof, B, C, C-inv, C-form, E, F, G, H, I, J, K). It runs only in
CI: the lab needs Docker, a bottle image built from the PR, IPv6 on a user
network, and `AF_PACKET` sockets.

## What the lab is

Everything runs from the one bottle image (`DJINN_CONTAINMENT_IMAGE`); the
repo's `tests/containment/` is mounted read-only at `/lab`.

| Container | Role |
|---|---|
| `sink` | recorder on the uplink: logs every IP packet that arrives (AF_PACKET) and listens on every probed port |
| `dns` | recording lab upstream DNS; logs every query, parsed or not |
| `decoy` | recorder standing in for the gateway, supervisor, Newt and `host.docker.internal` addresses |
| `b1`, `b2` | bottles; `b1` also runs a recorder with a listener on every probed port |
| `ctl` | control client outside any bottle policy, used only to prove the recorders see traffic |

The uplink is `11.a.b.0/24` (globally routable per `ipaddress`) because the
gateway will dial only global addresses; RFC 1918 space would be refused by
the very rule under test. Non-global and forbidden-set addresses exist as
aliases on the sink, routed there from the dialer.

## The oracle

A probe passes only if the recorders saw nothing: `lab_kit.silence`. The
baseline tests (`test_00`..`test_02`) send control traffic of every class and
require the recorders to see it, so a blind recorder fails the check. Run with
`CONTAINMENT_BLIND_RECORDER=1` to prove it: every baseline fails and every
expected failure becomes an unexpected success.

## Expected failures

Today no gateway exists, so each probe runs against the `no-gateway` variant
(today's bottle: flat network, `NET_ADMIN`/`NET_RAW`) and is an expected
failure. `lab_kit.UNMET_BY` is the ledger: probe -> the child plan whose PR
flips it. **Flipping a probe is part of that child's PR**: delete its entry,
add the lab variant it needs (`lab_env.Lab.VARIANTS`), and the probe must pass.

A probe that starts passing while still listed fails the run (unexpected
success). A lab that breaks while a probe is an expected failure does not hide
as one: it fails the run too (`lab_kit.probe`).

## CI

`ci-staged/ci.yml` is the live workflow plus a `containment` job (agents cannot
edit `.github/workflows/`). To land it:

1. Copy `ci-staged/ci.yml` over `.github/workflows/ci.yml` and commit.
2. Remove `ci-staged/` in a follow-up commit.
3. In the repo's branch protection for `main`, add the check named
   `containment` as required.
4. Open or update a PR; the `containment` job prints a table of every probe's
   result and uploads `containment-report.json`.

With `DJINN_CONTAINMENT_IMAGE` set, an unavailable Docker is a failure, never a
skip. Without it the lab tests skip and only `test_units.py` (docker-free) runs.

## Running

```
python3 -m unittest discover -s tests/containment -p 'test_units.py' -v      # no docker
docker build -t bottle . && DJINN_CONTAINMENT_IMAGE=bottle \
  python3 -m unittest discover -s tests/containment -p 'test_probes.py' -v   # needs Docker, root-capable
```
