#!/usr/bin/env python3
"""check_tokens.py: app code must style through the design tokens.

Scans .vue and .ts files under src/, skipping src/components/ui/ (generated
by shadcn-vue and owned there) and src/lib/. Fails on:

  arbitrary   Tailwind arbitrary values: text-[15px], w-[300px], left-[55%]
  palette     raw palette colours: emerald-600, bg-white, text-black
  spacing     numeric spacing or sizing: px-6, gap-3, size-4, w-32, -mt-1 (0 is allowed)
  type        the stock type scale: text-xs, text-sm, text-lg
  inline      style="..." / :style bindings and <style> blocks

Every one of those has a named replacement in src/styles/tokens.css. Prints
one `path:line: rule: match` per finding and exits 1 if there are any.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_DIRS = ("components/ui", "lib")
SUFFIXES = (".vue", ".ts")

_COLOURS = (
    "red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|"
    "purple|fuchsia|pink|rose|slate|gray|zinc|neutral|stone"
)
_SPACING_PREFIXES = (
    "p|px|py|ps|pe|pt|pr|pb|pl|m|mx|my|ms|me|mt|mr|mb|ml|gap|gap-x|gap-y|space-x|space-y|"
    "w|h|min-w|min-h|max-w|max-h|size|top|right|bottom|left|inset|inset-x|inset-y|"
    "translate-x|translate-y|leading"
)
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("arbitrary", re.compile(r"(?<![\w-])!?-?[a-z][\w-]*-\[[^\]\s]+\]")),
    ("palette", re.compile(rf"(?<![\w-])[a-z-]+-(?:(?:{_COLOURS})-\d{{2,3}}|white|black)(?:/\d+)?(?![\w-])")),
    ("spacing", re.compile(rf"(?<![\w-])!?-?(?:{_SPACING_PREFIXES})-(?:[1-9]\d*(?:\.\d+)?|0\.\d+)(?![\w./-])")),
    ("type", re.compile(r"(?<![\w-])text-(?:xs|sm|base|lg|\d?xl)(?![\w-])")),
    ("inline", re.compile(r"(?<![\w-]):?style=|<style[\s>]")),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    match: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.match}"


def is_excluded(path: Path, src: Path) -> bool:
    rel = path.relative_to(src).as_posix()
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDED_DIRS)


def scan_text(text: str, path: Path) -> list[Finding]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, pattern in RULES:
            for m in pattern.finditer(line):
                findings.append(Finding(path, lineno, rule, m.group(0)))
    return findings


def scan(src: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    files = 0
    for path in sorted(src.rglob("*")):
        if path.suffix not in SUFFIXES or not path.is_file() or is_excluded(path, src):
            continue
        files += 1
        findings.extend(scan_text(path.read_text(encoding="utf-8"), path))
    return findings, files


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    src = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "src"
    started = time.monotonic()
    findings, files = scan(src)
    for f in findings:
        print(f)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"check_tokens: scanned {files} files under {src} in {elapsed_ms} ms, {len(findings)} findings",
          file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
