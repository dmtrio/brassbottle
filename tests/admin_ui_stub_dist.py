"""A minimal admin UI build for tests that need the daemon to start but not the real app.

The daemon refuses to start without a dist directory holding index.html, so a test that
only cares about the API points DJINN_ADMIN_UI_DIST at one of these.
"""
from __future__ import annotations

from pathlib import Path

STUB_INDEX = "<!doctype html><html><body>SPA</body></html>"


def make_stub_dist(parent: Path) -> Path:
    """Write `<parent>/ui-dist/index.html` (idempotent) and return the directory."""
    dist = parent / "ui-dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(STUB_INDEX, encoding="utf-8")
    return dist
