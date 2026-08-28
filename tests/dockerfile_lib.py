"""Shared Dockerfile parsing for tests that assert on image layers."""

def instructions(text):
    """The Dockerfile's logical instructions, backslash-continuations joined.

    Splitting on `# ── ` section comments (the earlier approach) does NOT
    express the property under test: a purge relocated into a *separate* RUN
    inside the same commented section still reads as "after the loop", but
    lands in a later layer and shrinks nothing. Only real instruction
    boundaries distinguish those two cases.
    """
    out, cur = [], []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # A comment inside a continuation is stripped by the Dockerfile parser
        # and does NOT end the instruction (the repo relies on this — see the
        # "Keep pipefail" note inside the plugin bake loop).
        if stripped.startswith("#"):
            continue
        if not cur and not stripped:
            continue
        cur.append(line)
        if not line.endswith("\\"):
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out
