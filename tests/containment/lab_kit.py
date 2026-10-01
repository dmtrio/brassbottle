"""Host-side bookkeeping for the containment probes: recorder parsing, the
silence oracle, and the expected-failure ledger. No docker in this module, so
every rule here is unit-tested without a lab (test_units.py).
"""

import io
import json
import os
import sys
import time
import unittest
from dataclasses import dataclass, field

# Probe -> the child plan whose PR flips it (and deletes its entry here). The
# comment on each says what that child delivers that the probe is waiting for.
UNMET_BY = {
    "A-root": "16",    # cutover renders cap_drop NET_ADMIN/NET_RAW into the real bottle
    "A-spoof": "09",   # gateway raw PREROUTING anti-spoof rules
    "B": "16",         # cutover: same cap_drop as A-root (no NET_RAW => no AF_PACKET)
    "C": "11",         # edge: SNI/Host must equal the fake-IP's name
    "C-inv": "11",     # edge dial predicate (decide.py from child 03 is its input)
    "C-form": "11",    # edge: refuses absolute-form, CONNECT, no SNI/Host, SNI != name, UDP 443
    "E": "09",         # gateway ip6tables DROP everywhere
    "F": "10",         # edge resolver: upstream asked only for allowed zones
    "G": "10",         # same resolver, reached through 127.0.0.11
    "H": "09",         # gateway INPUT default DROP and per-bottle networks
    "I": "09",         # fail-closed boot, bootstrap rules
    "J": "09",         # supervisor's route invariant
    "K": "11",         # edge: per-bottle policy and fake-IP namespaces
}
PROBE_IDS = tuple(UNMET_BY)
SETTLE_SECONDS = 1.0
LOG_PREFIX = "[containment]"


def log(stage: str, msg: str) -> None:
    print(f"{LOG_PREFIX} {stage}: {msg}", file=sys.stderr, flush=True)


class Breach(AssertionError):
    """A recorder saw traffic the Contract forbids. `evidence` is never empty."""

    def __init__(self, probe: str, evidence):
        self.probe, self.evidence = probe, list(evidence)
        shown = "\n  ".join(self.evidence[:12])
        more = f"\n  ... and {len(self.evidence) - 12} more" if len(self.evidence) > 12 else ""
        super().__init__(f"probe {probe}: {len(self.evidence)} breach(es)\n  {shown}{more}")


# ── recorder events ──────────────────────────────────────────────────────────

def parse_events(text: str, source: str):
    """JSON-lines -> (events, dropped). A line that is not a JSON object is
    counted, never silently skipped: dropped > 0 is logged and fails the lab."""
    events, dropped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            dropped += 1
            continue
        if not isinstance(event, dict):
            dropped += 1
            continue
        event["source"] = source
        events.append(event)
    return events, dropped


def fmt_event(event: dict) -> str:
    where = event.get("source", "?")
    if event.get("kind") == "dns":
        return (f"{where}: dns {event.get('transport')} from {event.get('src')} "
                f"qname={event.get('qname')!r} qtype={event.get('qtype')} parsed={event.get('parsed')}"
                f" raw={event.get('raw_len')}B")
    proto = event.get("proto", "?")
    ports = f":{event.get('sport')} -> {event.get('dst')}:{event.get('dport')}" if "dport" in event \
        else f" -> {event.get('dst')}"
    extra = f" flags={event['flags']}" if event.get("flags") else ""
    pv = f" preview={event['preview'][:40]!r}" if event.get("preview") else ""
    return f"{where}: {proto} {event.get('src')}{ports}{extra}{pv}"


def traffic(events, since: int = 0, src=None, match=None):
    """Packet and DNS events after index `since`, optionally only from sources `src`
    (a predicate on the source address). Listener 'accept' lines are excluded:
    the packet that caused each is already in the log."""
    out = []
    for event in events[since:]:
        if event.get("kind") not in ("packet", "dns"):
            continue
        if src is not None and not src(event.get("src", "")):
            continue
        if match is not None and not match(event):
            continue
        out.append(event)
    return out


class Recorder:
    """One recorder process's event file, fetched on demand through `fetch`."""

    def __init__(self, name: str, fetch, blind: bool = False):
        self.name, self._fetch, self.blind = name, fetch, blind

    def snapshot(self):
        started = time.monotonic()
        text = self._fetch()
        events, dropped = parse_events(text, self.name)
        if dropped:
            raise AssertionError(f"recorder {self.name}: {dropped} unparseable event line(s)")
        if self.blind:  # CONTAINMENT_BLIND_RECORDER: prove a blind recorder fails the check
            events = []
        log("recorder", f"{self.name} read {len(text)}B parsed={len(events)} dropped={dropped} "
                        f"blind={self.blind} secs={time.monotonic() - started:.2f}")
        return events

    def mark(self) -> int:
        return len(self.snapshot())


def tagged(tag: str):
    """Predicate: the event carries `tag` in its payload preview or DNS name. Tags
    survive NAT-like rewriting of the source, which a gateway's uplink address won't."""
    return lambda event: tag in (event.get("preview") or "") or tag in (event.get("qname") or "")


def silence(probe: str, recorders, marks: dict, filters: dict = None, ignore=(), match=None):
    """Raise Breach if any recorder in `recorders` saw traffic since `marks[name]`.

    `filters` maps recorder name -> predicate on the packet source (default: any
    source not in `ignore`, the lab's own control addresses); `match` further
    restricts to events satisfying it (see `tagged`)."""
    filters = filters or {}
    evidence = []
    for rec in recorders:
        pred = filters.get(rec.name, lambda src: src not in ignore)
        evidence += [fmt_event(e) for e in traffic(rec.snapshot(), marks.get(rec.name, 0), pred, match)]
    log("oracle", f"{probe} silence check: breaches={len(evidence)}")
    if evidence:
        raise Breach(probe, evidence)


def missing_from(events, expectations):
    """Baseline helper: expectations is {label: predicate(event)}; returns the
    labels no event satisfies. A blind recorder misses every one."""
    return [label for label, pred in expectations.items() if not any(pred(e) for e in events)]


# ── expected-failure ledger ──────────────────────────────────────────────────

@dataclass
class ProbeResult:
    probe: str
    status: str            # pass | unmet | unexpected-pass | regression | harness-error
    unmet_by: str = ""
    evidence: list = field(default_factory=list)
    error: str = ""


RESULTS = []
HARNESS_ERRORS = []


def classify(probe: str, unmet_by, exc):
    """The only place that decides what a probe outcome means."""
    if exc is None:
        return ProbeResult(probe, "unexpected-pass" if unmet_by else "pass", unmet_by or "")
    if isinstance(exc, Breach) and exc.evidence:
        return ProbeResult(probe, "unmet" if unmet_by else "regression", unmet_by or "",
                           evidence=exc.evidence)
    return ProbeResult(probe, "harness-error", unmet_by or "", error=f"{type(exc).__name__}: {exc}")


def probe(name: str, results=None, harness_errors=None):
    """Mark a test as probe `name`. Its entry in UNMET_BY makes it an expected
    failure naming the flipping child; delete the entry to flip the probe.

    A probe whose lab setup broke must not hide as an "expected failure"
    (unittest counts any exception as one), so a non-Breach error under an
    expected failure returns normally (an *unexpected success*, which fails the
    run) and is listed by assert_no_harness_errors in tearDownClass."""
    results = RESULTS if results is None else results
    harness_errors = HARNESS_ERRORS if harness_errors is None else harness_errors
    if name not in UNMET_BY:
        raise KeyError(f"unknown probe {name!r}")

    def decorate(fn):
        unmet_by = UNMET_BY[name]

        def wrapper(self):
            exc = None
            try:
                fn(self)
            except Exception as caught:  # noqa: BLE001 — classified below
                exc = caught
            res = classify(name, unmet_by, exc)
            results.append(res)
            log("probe", f"{name} -> {res.status}"
                         + (f" (child {res.unmet_by})" if res.unmet_by else "")
                         + f" evidence={len(res.evidence)}")
            if res.status == "harness-error":
                harness_errors.append(res)
                if unmet_by:
                    return  # unexpected success: loud, instead of a silent expected failure
                raise exc
            if res.status in ("unmet", "regression"):
                raise exc

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        if unmet_by:
            wrapper.__unittest_expecting_failure__ = True
        return wrapper

    return decorate


def assert_no_harness_errors(harness_errors=None) -> None:
    errors = HARNESS_ERRORS if harness_errors is None else harness_errors
    if errors:
        raise AssertionError("probe harness errors (setup broke, not a verdict):\n" +
                             "\n".join(f"  {e.probe}: {e.error}" for e in errors))


def report_table(results) -> str:
    rows = [f"{'probe':8} {'result':16} {'flipped by':10} breaches"]
    for res in sorted(results, key=lambda r: PROBE_IDS.index(r.probe) if r.probe in PROBE_IDS else 99):
        child = f"child {res.unmet_by}" if res.unmet_by else "-"
        rows.append(f"{res.probe:8} {res.status:16} {child:10} {len(res.evidence)}")
    return "\n".join(rows)


def write_report(results, path: str) -> None:
    payload = [{"probe": r.probe, "status": r.status, "unmet_by_child": r.unmet_by or None,
                "breaches": len(r.evidence), "evidence": r.evidence[:20], "error": r.error}
               for r in results]
    text = json.dumps(payload, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    log("report", f"wrote {path} {len(text)}B probes={len(payload)}")


def emit_report(results, path=None) -> None:
    """Print the per-probe table (the CI log is the Evidence) and write the JSON."""
    print("\ncontainment probe results\n" + report_table(results), flush=True)
    write_report(results, path or os.environ.get("CONTAINMENT_REPORT", "/tmp/containment-report.json"))


def run_wrapped(case_cls):
    """Unit-test helper: run one TestCase class, return the unittest result."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_cls)
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
