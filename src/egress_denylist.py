#!/usr/bin/env python3
"""egress_denylist.py — persistent egress deny list (host-side + CLI).

Owns $DJINN_HOME/run/egress/denylist.json: the set of zones the operator has
decided to stop being asked about, scoped per-bottle or globally. This is a
SHORT-CIRCUIT ahead of the broker's approval queue, not a firewall — the
firewall's allowed-domains ipset stays the sole authority on whether traffic
passes (src/egress_log.py's invariant). The denylist only decides whether to
ask; egress_broker_host.EgressBroker consults it before filing a new request.

Also the entry point for `./djinn deny` / `./djinn deny --list` / `./djinn
undeny` (djinn is pure bash glue; all logic lives here) and for
bin/allow-egress.sh's `--check` probe. Stdlib only; host-side.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# egress_log is a LEAF module (stdlib imports only — see its own docstring),
# so importing _iso_ts from it here creates no cycle: this is the
# same direction this module already imports in (see egress_broker_host's
# top, which imports EgressLog from here too). Do not duplicate these two
# implementations a third time.
from egress_log import _iso_ts

LOG = logging.getLogger(__name__)

DENYLIST_FILENAME = "denylist.json"
DENYLIST_VERSION = 1

VALID_DECIDE_SCOPES = frozenset({"once", "bottle", "global"})

# Exit-code taxonomy for the `--check` probe (bin/allow-egress.sh depends on
# these exact values). `--check` now takes one-or-more domains and prints
# its own operator-facing lines (finding #10), so a normal run — covered or
# not, for any/all of the domains — exits 0; EXIT_CHECK_NOT_COVERED is kept
# only for the exit-code taxonomy tests below (EXIT_NOT_FOUND ==
# EXIT_CHECK_NOT_COVERED) and is not produced by `--check` itself any more.
# EXIT_CHECK_CORRUPT (finding #8) is: a corrupt denylist.json must not fail
# silently open — --check exits nonzero so bin/allow-egress.sh's caller-side
# `|| ...` fallback surfaces it as a named failure, instead of every domain
# silently reading as "not covered".
EXIT_CHECK_NOT_COVERED = 3
EXIT_CHECK_CORRUPT = 4
# CLI exit codes: 0 success, 1 refused-to-touch-a-corrupt-file (data integrity,
# distinct from a usage mistake), 2 usage error, 3 not-found — reusing the
# same "3" as EXIT_CHECK_NOT_COVERED since both mean "no matching entry".
EXIT_CORRUPT = 1
EXIT_USAGE_ERROR = 2
EXIT_NOT_FOUND = 3
DENYLIST_LOCK_FILENAME = "denylist.lock"


class DenyListError(Exception):
    """Invalid denylist operation (unknown bottle scope, bad zone, ...)."""


@dataclass(frozen=True)
class DenyEntry:
    """One persisted deny-list entry."""

    zone: str
    scope: str  # "global" or a bottle name
    created_at: str
    by: str = "operator"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "zone": self.zone,
            "scope": self.scope,
            "created_at": self.created_at,
            "by": self.by,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _parse_entry(raw: object) -> DenyEntry | None:
    if not isinstance(raw, dict):
        return None
    zone = raw.get("zone")
    scope = raw.get("scope")
    created_at = raw.get("created_at")
    if not isinstance(zone, str) or not zone:
        return None
    if not isinstance(scope, str) or not scope:
        return None
    if not isinstance(created_at, str):
        created_at = _iso_ts(None)
    by = raw.get("by")
    if not isinstance(by, str) or not by:
        by = "operator"
    reason = raw.get("reason")
    if not isinstance(reason, str):
        reason = None
    return DenyEntry(zone=zone, scope=scope, created_at=created_at, by=by, reason=reason)


def host_covered_by_zone(host: str, zone: str) -> bool:
    """Return True when host is zone itself or a subdomain of zone."""
    return host == zone or host.endswith("." + zone)


class DenyList:
    """Load/save $DJINN_HOME/run/egress/denylist.json; reload on mtime change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(DENYLIST_LOCK_FILENAME)
        # Guards every read/write of self._entries/_mtime (and the diagnosis
        # fields derived from them) against concurrent THREADS in the same
        # process — e.g. egress_watch's main thread (via
        # format_denylist_status -> load()) and a broker HTTP handler thread
        # (matches(), persist_deny -> add()+load()) sharing this one
        # instance (finding #1). This is independent of _file_lock's fcntl
        # flock below: flock is associated with the OPEN FILE DESCRIPTION,
        # so two threads in the SAME process each os.open()-ing the lock
        # file get two independent descriptions and do NOT exclude each
        # other — only cross-PROCESS callers (the daemon vs. a `./djinn
        # deny` CLI invocation) are serialized by that. Without this RLock,
        # _reload could set self._mtime before self._entries (the exact bug
        # here): a watcher thread's stale read could win the race and get
        # cached under the NEWER mtime, so the entry a handler thread just
        # persisted would not be enforced again until the file next changes.
        # RLock (not Lock): add()/remove() call _reload()/_persist() while
        # already holding it themselves — must be reentrant for the same
        # thread.
        self._lock = threading.RLock()
        self._entries: list[DenyEntry] = []
        self._mtime: float | None = None
        self._loaded = False
        self._corrupt: str | None = None
        # mtime (of the on-disk file) as of the last WARNING logged by
        # matches() for a corrupt file — gates that log to once per mtime
        # (i.e. once per distinct corruption) instead of once per consult;
        # matches() is called on every egress request, so ungated this was a
        # louder version of the exact log flood the coalesce window on the
        # short-circuit path exists to prevent.
        self._corrupt_warned_mtime: float | None = None
        self._reload(force=True)

    @property
    def corrupt(self) -> str | None:
        """Diagnosis from the last reload, or None when the file parsed
        cleanly (a missing file is not corruption — it is just empty)."""
        return self._corrupt

    def _current_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Hold an exclusive flock across load->mutate->os.replace.

        Sibling denylist.lock (never the data file itself, so a lock holder
        never blocks a plain read/matches() elsewhere). Without this, the
        daemon process and a `./djinn deny` CLI process can interleave a
        read-modify-write and silently drop one or the other's entry.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _reload(self, *, force: bool = False) -> None:
        """Reload from disk when the file's mtime changed (or force=True).

        Mirrors egress_broker_host.BottleTokenStore._reload: cheap to call on
        every consult, corrupt/missing file is never fatal to READ (matches()
        just treats it as empty) — but add()/remove() refuse to write over a
        diagnosed-corrupt file; see self.corrupt.

        Whole body under self._lock (finding #1): self._entries and
        self._mtime must be updated as one atomic step from another thread's
        point of view, never observed half-updated (mtime already the new
        value, entries still the old list, or vice versa).
        """
        with self._lock:
            mtime = self._current_mtime()
            if not force and self._loaded and mtime == self._mtime:
                return
            self._mtime = mtime
            self._loaded = True
            self._corrupt = None
            if mtime is None:
                self._entries = []
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._corrupt = str(exc)
                # INFO, not WARNING: this is the load-time diagnosis, gated
                # only by mtime-changed-since-last-_reload (i.e. it can fire
                # on every distinct corrupt edit). matches() below has its
                # OWN louder WARNING for the same condition, gated to once
                # per distinct mtime via self._corrupt_warned_mtime — this
                # line staying at INFO avoids double-logging the same event
                # at WARNING through two independently-gated paths.
                LOG.info(
                    "egress denylist unreadable path=%s error=%s",
                    self._path.name,
                    exc,
                )
                self._entries = []
                return
            if not isinstance(raw, dict):
                self._corrupt = "not a JSON object"
                LOG.info(
                    "egress denylist unreadable path=%s reason=not_an_object",
                    self._path.name,
                )
                self._entries = []
                return
            entries: list[DenyEntry] = []
            for item in raw.get("entries") or []:
                entry = _parse_entry(item)
                if entry is not None:
                    entries.append(entry)
            self._entries = entries
            # DEBUG, not INFO (finding #6): this runs on every consult
            # (matches() reloads on every egress request), so at INFO it
            # floods the daemon's -v output and, worse, main()'s old
            # INFO-by-default made every `./djinn allow`/`deny`/`undeny`
            # print this line to the terminal for no operator-relevant
            # reason. A corrupt/unreadable file (above) stays loud —
            # unlike a routine successful load, that IS worth surfacing.
            LOG.debug(
                "egress denylist load enter path=%s count=%d",
                self._path.name,
                len(self._entries),
            )

    def load(self) -> list[DenyEntry]:
        """Force a fresh read and return all entries (CLI `list`)."""
        with self._lock:
            self._reload(force=True)
            return list(self._entries)

    def matches(self, container: str, host: str) -> DenyEntry | None:
        """Return the best-matching entry for (container, host), or None.

        Reloads on mtime change first, so a CLI edit takes effect without a
        daemon restart. Longest zone wins when more than one entry covers
        the host. Ties (same zone length, one global and one bottle-scoped)
        prefer the bottle-scoped entry — deterministically, independent of
        on-disk/list order: the operator who scoped a deny to THIS bottle
        specifically should see that entry reported, not whichever one
        happened to load first. A corrupt file is treated as empty here too
        (never blocks traffic decisions), but at WARNING — louder than the
        INFO load-time diagnosis — since a corrupt denylist silently means
        "nothing is denied any more".
        """
        with self._lock:
            self._reload()
            if self._corrupt is not None:
                if self._mtime != self._corrupt_warned_mtime:
                    LOG.warning(
                        "egress denylist matches against corrupt file path=%s error=%s "
                        "— treating as empty (nothing is denylisted until it is fixed)",
                        self._path.name,
                        self._corrupt,
                    )
                    self._corrupt_warned_mtime = self._mtime
                return None
            best: DenyEntry | None = None
            for entry in self._entries:
                if entry.scope != "global" and entry.scope != container:
                    continue
                if not host_covered_by_zone(host, entry.zone):
                    continue
                if best is None or len(entry.zone) > len(best.zone):
                    best = entry
                elif (
                    len(entry.zone) == len(best.zone)
                    and best.scope == "global"
                    and entry.scope != "global"
                ):
                    best = entry
            return best

    def _refuse_if_corrupt(self) -> None:
        if self._corrupt is not None:
            raise DenyListError(
                f"denylist.json unreadable ({self._corrupt}); fix it or move it "
                "aside — refusing to overwrite"
            )

    def _persist(self, entries: list[DenyEntry]) -> None:
        with self._lock:
            payload = {
                "version": DENYLIST_VERSION,
                "entries": [e.to_dict() for e in entries],
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
            text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            try:
                tmp_path.write_text(text, encoding="utf-8")
                os.replace(tmp_path, self._path)
            except OSError:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
            self._entries = entries
            self._mtime = self._current_mtime()
            self._loaded = True
            self._corrupt = None
            LOG.info(
                "egress denylist save exit path=%s count=%d",
                self._path.name,
                len(entries),
            )

    def add(
        self,
        *,
        zone: str,
        scope: str,
        reason: str | None = None,
        by: str = "operator",
        now: datetime | None = None,
    ) -> DenyEntry:
        """Add (or replace) an entry for (zone, scope); atomic write.

        Raises DenyListError (never overwrites) when the on-disk file is
        diagnosed corrupt, and OSError on a genuine disk failure — both are
        the caller's to handle (egress_broker_host.decide()/persist_deny()
        catch and degrade to a one-shot deny; the CLI reports and exits 1).
        """
        with self._lock, self._file_lock():
            self._reload(force=True)
            self._refuse_if_corrupt()
            entry = DenyEntry(
                zone=zone,
                scope=scope,
                created_at=_iso_ts(now),
                by=by,
                reason=reason,
            )
            remaining = [
                e for e in self._entries if not (e.zone == zone and e.scope == scope)
            ]
            remaining.append(entry)
            LOG.info(
                "egress denylist add enter zone=%s scope=%s",
                zone,
                scope,
            )
            self._persist(remaining)
        return entry

    def remove(self, *, zone: str, scope: str) -> bool:
        """Remove the (zone, scope) entry; return True when one was removed.

        Same corrupt-file refusal as add() — see its docstring.
        """
        with self._lock, self._file_lock():
            self._reload(force=True)
            self._refuse_if_corrupt()
            remaining = [
                e for e in self._entries if not (e.zone == zone and e.scope == scope)
            ]
            removed = len(remaining) != len(self._entries)
            LOG.info(
                "egress denylist remove enter zone=%s scope=%s found=%s",
                zone,
                scope,
                removed,
            )
            if removed:
                self._persist(remaining)
        return removed


def validate_bottle_scope(container: str, tokens_dir: Path) -> None:
    """Raise DenyListError when `container` is not a valid bottle name.

    This is ONLY ever called with an actual bottle name — never with the
    reserved "global" scope keyword itself (callers skip this check
    entirely for a true global-scope write, since there is no bottle name
    to validate there). "global" is therefore always rejected here: a
    bottle literally named "global" would otherwise collide with the
    global scope's own on-disk representation (scope="global"), silently
    turning a scope=bottle write into a de-facto global deny. Otherwise
    container must be a bottle name with a token at tokens/<container>.token
    — same posture as an unknown secret in up.sh: a hard error, not a
    silent no-op.
    """
    if container == "global":
        raise DenyListError(
            "'global' is a reserved scope name, not a valid bottle name — "
            "pass --global instead of --bottle global"
        )
    token_path = tokens_dir / f"{container}.token"
    if not token_path.is_file():
        raise DenyListError(
            f"unknown bottle {container!r} (no token at tokens/{container}.token — "
            "has it ever filed an egress request?)"
        )


# ── host-side wiring (base path / egress root) ──
# This module is the single owner of _repo_root/resolve_base_path/
# resolve_egress_root/host_covered_by_zone: egress_broker_host imports and
# re-exports them under the same names rather than keeping its own copies.
# Safe in this direction only — egress_broker_host imports FROM this module
# at its own module top (DenyList etc.), so this module must never import
# egress_broker_host at module top itself (its remaining uses of
# egress_broker_host, below, are all lazy/inside functions).


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_base_path(raw: str) -> Path:
    # expanduser throughout: ./.env is sourced by bash, so DJINN_HOME="$HOME/x"
    # arrives already expanded, but a literal ~/x does not — and Path("~/x")
    # would quietly create a directory actually named "~" in the cwd. That is
    # the kind of thing you only notice when the queue turns out to be empty.
    if raw:
        return Path(raw).expanduser()
    env = os.environ.get("DJINN_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return _repo_root() / ".djinn"


def resolve_egress_root(base_path: Path) -> Path:
    return base_path / "run" / "egress"


def _denylist_for(base_path: Path) -> tuple[DenyList, Path]:
    egress_root = resolve_egress_root(base_path)
    return DenyList(egress_root / DENYLIST_FILENAME), egress_root


# ── operator-daemon POST (the daemon is the ONE place that writes an entry) ──


def _try_daemon_deny(
    egress_root: Path,
    *,
    zone: str,
    scope: str,
    container: str | None,
    reason: str | None,
) -> tuple[int, dict[str, Any]] | None:
    """POST /decide {decision: deny, scope, host: zone, container?, reason?}
    to the running daemon — the ONLY place a deny-list entry is actually
    written (EgressBroker.persist_deny, under its lock; it also sweeps any
    open requests the entry now covers, WITH denylist context, which a bare
    file write from here could never do).

    Returns (status_code, parsed_json_body) for any response the daemon
    actually sent — including a 400 or a 5xx, so the caller can tell a hard
    validation error (400: e.g. an unknown bottle) from a failure that
    should fall back to a direct file write. Returns None only when there
    was no way to even ask: no operator token on disk yet (daemon never
    started), or a connection-level failure (refused, DNS, timeout) — the
    caller degrades to writing denylist.json directly in either case.
    """
    from egress_broker_host import OPERATOR_TOKEN_FILENAME, daemon_base_url

    token_path = egress_root / OPERATOR_TOKEN_FILENAME
    if not token_path.is_file():
        LOG.info("egress denylist cli daemon skip reason=no_operator_token")
        return None
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOG.info("egress denylist cli daemon skip reason=token_unreadable error=%s", exc)
        return None
    if not token:
        LOG.info("egress denylist cli daemon skip reason=empty_token")
        return None

    base_url = daemon_base_url(egress_root)
    payload: dict[str, Any] = {"decision": "deny", "scope": scope, "host": zone}
    if container is not None:
        payload["container"] = container
    if reason is not None:
        payload["reason"] = reason
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/decide",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    started = time.monotonic()
    # Boundary-out log with the request size (finding #10) — paired with the
    # "done" line below (boundary-in), which adds resp_bytes once a response
    # (or HTTPError body) actually comes back. Pattern: NtfyNotifier.send in
    # egress_notify.py.
    LOG.info(
        "egress denylist cli daemon dispatch url=%s bytes=%d",
        base_url,
        len(body),
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        LOG.info(
            "egress denylist cli daemon done url=%s status=unreachable duration_ms=%d error=%s",
            base_url,
            int((time.monotonic() - started) * 1000),
            exc,
        )
        return None

    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    LOG.info(
        "egress denylist cli daemon done url=%s status=%d duration_ms=%d req_bytes=%d resp_bytes=%d",
        base_url,
        status,
        duration_ms,
        len(body),
        len(raw),
    )
    return status, parsed


# ── CLI ──


def _normalize_zone(raw: str) -> str:
    """Normalize a CLI-supplied zone (domain OR IP literal); lazy import
    for the same circular-import reason as the daemon-port lookup below
    (egress_broker_host imports this module at its own top).

    Delegates to normalize_destination (not normalize_host, which is
    domain-only) so `93.0.2.55`, `93.0.2.55:443`, `http://93.0.2.55`, and
    `[::1]:443` all normalize the same way the daemon's /decide handler
    accepts them — a CLI zone that the daemon would then reject is exactly
    the kind of drift two divergent normalizers used to produce.
    """
    from egress_broker_host import normalize_destination

    try:
        host, _is_ip = normalize_destination(raw)
    except ValueError:
        raise DenyListError(f"not a valid domain or IP: {raw!r}") from None
    return host


def _scope_from_args(args: argparse.Namespace) -> str:
    if args.global_scope and args.bottle:
        raise DenyListError("pass exactly one of --bottle NAME or --global, not both")
    if not args.global_scope and not args.bottle:
        raise DenyListError("scope is required: pass --bottle NAME or --global")
    return "global" if args.global_scope else args.bottle


def _format_denied(zone: str, scope: str, reason: str | None) -> str:
    """The operator-facing "Denied: ..." line — used by BOTH _cmd_add's
    daemon-success path and its direct-write fallback (finding #8), so the
    two must never drift into two subtly different wordings of the same
    outcome."""
    return f"Denied: {zone} (scope={scope})" + (f" — {reason}" if reason else "")


def _cmd_add(args: argparse.Namespace) -> int:
    from egress_broker_host import TOKENS_DIRNAME, daemon_base_url

    base_path = resolve_base_path(args.base_path)
    denylist, egress_root = _denylist_for(base_path)
    try:
        zone = _normalize_zone(args.zone)
        write_scope = _scope_from_args(args)  # "global" or a bottle name
    except DenyListError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    reason = args.reason or None
    is_global = write_scope == "global"
    decide_scope = "global" if is_global else "bottle"
    container = None if is_global else write_scope

    # Daemon-first: it writes under its own lock, validates the bottle
    # against ITS OWN root, and sweeps any open requests the entry now
    # covers (with denylist context) in the same call — a direct file write
    # from here cannot do that sweep at all. Crucially, this also means the
    # daemon's answer is asked for BEFORE any local validate_bottle_scope
    # check (finding #3): if $DJINN_HOME diverges between this CLI
    # invocation and the running daemon, the daemon's own view of which
    # bottles exist must win — a bottle the daemon knows must never be
    # rejected here just because this process resolved a different root.
    daemon_result = _try_daemon_deny(
        egress_root, zone=zone, scope=decide_scope, container=container, reason=reason
    )
    if daemon_result is not None:
        status, payload = daemon_result
        if status == 200:
            persisted = payload.get("persisted")
            persisted = persisted if isinstance(persisted, dict) else {}
            decided = payload.get("decided")
            decided = decided if isinstance(decided, list) else []
            entry_zone = persisted.get("zone", zone)
            entry_scope = persisted.get("scope", write_scope)
            print(_format_denied(entry_zone, entry_scope, reason))
            if decided:
                print(f"Closed {len(decided)} open request(s) held on this zone.")
            return 0
        if status == 400:
            message = payload.get("error", "bad request")
            print(f"Error: {message}", file=sys.stderr)
            return EXIT_USAGE_ERROR
        # Any OTHER status the daemon actually answered (401/403/5xx/...) is
        # a hard error too — never a fallback. Falling back here would mean
        # writing the file directly UNDER the daemon's nose while it is up
        # and reachable (it just refused this request for its own reason,
        # e.g. auth), silently bypassing whatever made it refuse, and
        # skipping the sweep of any held requests only the daemon can do.
        # Only a genuine connection-level failure (see _try_daemon_deny:
        # URLError/refused/timeout, or no operator token on disk) may fall
        # through to the direct-write path below.
        message = payload.get("error", "no error detail")
        print(f"Error: daemon answered {status}: {message}", file=sys.stderr)
        return EXIT_CORRUPT

    # Daemon genuinely unreachable (or never started) — this process is on
    # its own, so it must validate the bottle itself before writing
    # directly. Only the --bottle path needs bottle-name validation:
    # --global's write_scope is also the literal string "global", but that
    # is the RESERVED scope keyword, not a bottle name, so it must never be
    # passed to validate_bottle_scope (which rejects "global" as a bottle
    # name — see its docstring, finding #4: a bottle literally named
    # "global" must be rejected, not silently aliased to the global scope).
    if not args.global_scope:
        try:
            validate_bottle_scope(write_scope, egress_root / TOKENS_DIRNAME)
        except DenyListError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR

    try:
        entry = denylist.add(zone=zone, scope=write_scope, reason=reason)
    except (DenyListError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CORRUPT
    print(_format_denied(entry.zone, entry.scope, entry.reason))
    # Name the address that was tried (finding: an operator staring at "not
    # reachable" has no way to tell WHICH endpoint was dead — a non-default
    # --port/--host daemon.json, or just none running at all).
    print(
        f"daemon not reachable at {daemon_base_url(egress_root)}; wrote "
        "denylist.json directly — held requests were NOT swept",
        file=sys.stderr,
    )
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    from egress_broker_host import TOKENS_DIRNAME

    base_path = resolve_base_path(args.base_path)
    denylist, egress_root = _denylist_for(base_path)
    try:
        zone = _normalize_zone(args.zone)
        scope = _scope_from_args(args)
        # Same guard as _cmd_add's fallback path (finding #2): --bottle
        # global must never resolve to the global scope's own on-disk
        # representation, and an unknown bottle is a hard error rather than
        # a silent no-op that just happens to remove nothing. _cmd_remove
        # has no daemon path at all (see test_remove_never_talks_to_the_
        # daemon) — undeny only widens what's asked about again, so there is
        # nothing to sweep and nothing a daemon-first check would buy here;
        # this validates directly, every time.
        if not args.global_scope:
            validate_bottle_scope(scope, egress_root / TOKENS_DIRNAME)
    except DenyListError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        removed = denylist.remove(zone=zone, scope=scope)
    except (DenyListError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CORRUPT
    if not removed:
        print(f"No denylist entry for zone={zone} scope={scope}", file=sys.stderr)
        return EXIT_NOT_FOUND
    print(f"Undenied: {zone} (scope={scope})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    base_path = resolve_base_path(args.base_path)
    denylist, _egress_root = _denylist_for(base_path)
    # DenyList.load() never raises (a corrupt/missing file is folded into
    # denylist.corrupt / an empty list, not an exception) — no try/except
    # needed here; the corrupt check right below is the real guard.
    entries = sorted(denylist.load(), key=lambda e: (e.zone, e.scope))
    if denylist.corrupt is not None:
        print(f"Error: denylist.json unreadable ({denylist.corrupt})", file=sys.stderr)
        return EXIT_CORRUPT
    if not entries:
        print("(no denylist entries)")
        return 0
    for entry in entries:
        line = f"{entry.zone:<30} scope={entry.scope:<20} created={entry.created_at}"
        if entry.reason:
            line += f"  reason={entry.reason}"
        print(line)
    return 0


def _cmd_check(rest: list[str]) -> int:
    """bin/allow-egress.sh's `--check <bottle> <domain> [<domain> ...]`
    probe (finding #10): accepts one-or-more domains in a single call and
    PRINTS its own operator-facing warning lines — bash no longer builds
    them, it just invokes this once with every domain and surfaces a
    nonzero exit as a named failure.

    Exits EXIT_CHECK_CORRUPT (4) when denylist.json itself is corrupt
    (finding #8: this must not read as "nothing is covered" — a genuine
    diagnosis goes to stderr and the caller's `|| ...` fallback fires) or
    EXIT_USAGE_ERROR (2) for a malformed domain; otherwise exits 0 whether
    or not any domain was covered — coverage is reported by the printed
    lines themselves, not by the exit code.
    """
    parser = argparse.ArgumentParser(prog="egress_denylist.py --check")
    parser.add_argument("bottle")
    parser.add_argument("domain", nargs="+")
    parser.add_argument("--base-path", default="")
    args = parser.parse_args(rest)

    base_path = resolve_base_path(args.base_path)
    denylist, _egress_root = _denylist_for(base_path)

    # One diagnosis for the whole batch: a corrupt file affects every
    # domain identically, so check it up front rather than per domain.
    denylist.load()
    if denylist.corrupt is not None:
        print(
            f"deny-list check failed: denylist.json unreadable ({denylist.corrupt}) "
            "— entries NOT applied, treating as not covered",
            file=sys.stderr,
        )
        return EXIT_CHECK_CORRUPT

    for domain in args.domain:
        try:
            host = _normalize_zone(domain)
        except DenyListError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR
        entry = denylist.matches(args.bottle, host)
        if entry is None:
            continue
        line = f"⚠ {domain} is covered by a persistent deny-list entry: {entry.zone} (scope={entry.scope})"
        if entry.reason:
            line += f" — {entry.reason}"
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="djinn persistent egress deny list (host-side)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="add a deny entry")
    add_p.add_argument("zone")
    add_p.add_argument("--bottle", default=None, metavar="NAME")
    add_p.add_argument("--global", dest="global_scope", action="store_true")
    add_p.add_argument("--reason", default="")
    add_p.add_argument("--base-path", default="")
    add_p.set_defaults(func=_cmd_add)

    remove_p = sub.add_parser("remove", help="remove a deny entry")
    remove_p.add_argument("zone")
    remove_p.add_argument("--bottle", default=None, metavar="NAME")
    remove_p.add_argument("--global", dest="global_scope", action="store_true")
    remove_p.add_argument("--base-path", default="")
    remove_p.set_defaults(func=_cmd_remove)

    list_p = sub.add_parser("list", help="list deny entries")
    list_p.add_argument("--base-path", default="")
    list_p.set_defaults(func=_cmd_list)

    return parser


def _extract_verbosity(argv: list[str]) -> tuple[int, list[str]]:
    """Pull every -v/--verbose (bundled short form -vv/-vvv/... included) out
    of argv; return (count, argv with those flags removed).

    Done up front, before EITHER dispatch path below (the bare `--check`
    bypass, which has its own argparse with no -v of its own, and
    build_parser()'s subcommands) — so -v works regardless of where the
    caller puts it, and so the log level is set before the first DenyList()
    construction, which logs on load (finding #6).
    """
    # Options that consume the next token: a value of "-v" (e.g.
    # `--reason -v`) is data, not a verbosity flag, and swallowing it would
    # make argparse fail with "expected one argument" or silently steal an
    # unrelated later token as the value.
    value_taking = {"--reason", "--bottle", "--base-path"}
    count = 0
    rest: list[str] = []
    prev = ""
    for arg in argv:
        takes_value, prev = prev in value_taking, arg
        if takes_value:
            rest.append(arg)
        elif arg == "-v" or arg == "--verbose":
            count += 1
        elif len(arg) > 1 and arg[0] == "-" and set(arg[1:]) == {"v"}:
            count += len(arg) - 1
        else:
            rest.append(arg)
    return count, rest


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    verbosity, raw_argv = _extract_verbosity(raw_argv)
    # Default WARNING, not INFO (finding #6): `./djinn allow` (via
    # bin/allow-egress.sh's --check probe) and `./djinn deny`/`undeny` used
    # to print every LOG.info line (route loads, add/remove/save
    # bookkeeping) to the terminal for no operator-relevant reason. Mirrors
    # egress_watch's own -v/-vv convention.
    level = (
        logging.DEBUG
        if verbosity > 1
        else logging.INFO
        if verbosity == 1
        else logging.WARNING
    )
    logging.basicConfig(level=level, format="%(message)s")

    if raw_argv and raw_argv[0] == "--check":
        return _cmd_check(raw_argv[1:])

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
