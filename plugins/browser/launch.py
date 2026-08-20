#!/usr/bin/env python3
"""Per-container browser launcher (host-side).

Invoked via ./service.sh browser <container> [brave|chrome]. Reads the bridge
port from the container manifest (plugin_ports.browser) with a fallback to
host_port in plugins/browser/plugin.yml. CDP port is offset from the bridge
port so one manifest field controls both: CDP = bridge - 8814 + 9222.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

# Mirrors service.sh / manifest.py's name rule (letters, digits, _, -).
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")

BRIDGE_PORT_DEFAULT = 8814
BRIDGE_PORT_BASE = 8814
CDP_PORT_BASE = 9222

# The secret slot plugins/browser/plugin.yml declares.
SLOT = "RESEARCH_BROWSER_KEY"

# Default install locations. Override per setup with BRAVE_APP / CHROME_APP in
# ./.env (the repo's existing host-side override channel, alongside
# DJINN_HOME / RULES_PATH / BOTTLES_PATH) — for a browser in
# ~/Applications, a renamed app bundle, or a Chromium variant. A one-off can
# also pass an absolute path in place of brave|chrome.
BRAVE_APP_DEFAULT = "/Applications/Brave Browser.app"
CHROME_APP_DEFAULT = "/Applications/Google Chrome.app"


def run_tool(cmd, **kw):
    """subprocess.run that turns a missing binary into the repo's ERROR: form.

    yq / open / bash / npx are all host prerequisites; a bare FileNotFoundError
    traceback would read as a bug in this script rather than a missing tool.
    """
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError:
        raise SystemExit(f"ERROR: required command not found on PATH: {cmd[0]}")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def bottles_dir(base_path: str, env_override: str | None = None) -> Path:
    """Mirror src/common.sh BOTTLES_PATH resolution."""
    if env_override:
        return Path(env_override)
    custom = Path(base_path) / "bottles"
    if custom.is_dir():
        return custom
    return repo_root() / "bottles"


def resolve_bridge_port(manifest: dict, plugin_host_port: int = BRIDGE_PORT_DEFAULT) -> int:
    """Bridge port from manifest plugin_ports.browser, else plugin.yml host_port."""
    plugin_ports = manifest.get("plugin_ports")
    if isinstance(plugin_ports, dict):
        port = plugin_ports.get("browser")
        # manifest.py validates this at `up` time, but the launcher reads the
        # manifest directly — a hand-edited non-integer must not silently
        # become a bogus port on the command line.
        if port is not None:
            if isinstance(port, bool) or not isinstance(port, int):
                raise SystemExit(
                    f"ERROR: plugin_ports.browser must be an integer port number (got {port!r})")
            return port
    return plugin_host_port


def cdp_port(bridge_port: int) -> int:
    """CDP debug port offset from bridge: default 8814 → 9222."""
    return bridge_port - BRIDGE_PORT_BASE + CDP_PORT_BASE


def validate_bridge_port(bridge_port: int) -> int:
    """Bridge ports must stay below the CDP band.

    Every container's debug port is bridge+408, so the debug ports all live at
    CDP_PORT_BASE and above. Keeping every bridge below that line makes the two
    sets provably disjoint — otherwise a bridge at 9222 would silently attach
    to the default container's browser instead of its own.
    """
    if not 1 <= bridge_port <= 65535:
        raise SystemExit(f"ERROR: bridge port {bridge_port} out of range (1-65535)")
    if bridge_port >= CDP_PORT_BASE:
        raise SystemExit(
            f"ERROR: bridge port {bridge_port} must be below {CDP_PORT_BASE} — "
            f"debug ports are derived as bridge+{CDP_PORT_BASE - BRIDGE_PORT_BASE}, "
            "so a bridge in that band collides with another container's browser")
    return bridge_port


def profile_dir(base_path: str, container: str) -> Path:
    return Path(base_path) / "browser-profiles" / container


def browser_tmp_dir(base_path: str, container: str) -> Path:
    """Host TMPDIR for chrome-devtools-mcp; bind-mounted at /artifacts/browser."""
    return Path(base_path) / "browser-tmp" / container


def api_key_var(container: str, manifest: dict | None = None) -> str:
    """Which secrets.env variable holds this container's bridge key.

    The manifest's common_secrets binding is authoritative — that is the value
    up.sh wires into the container, so reading it here keeps the bridge and the
    container on the same key by construction. The derived fallback is only for
    a manifest that leaves the slot unbound; note it is not injective ('a-b' and
    'a_b' both fold to 'a_b'), which is the other reason to prefer the binding.
    """
    common = (manifest or {}).get("common_secrets")
    if isinstance(common, dict):
        bound = common.get(SLOT)
        if isinstance(bound, str) and bound:
            return bound
    elif isinstance(common, list) and SLOT in common:
        return SLOT
    return f"{SLOT}_{container.replace('-', '_')}"


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"ERROR: file not found: {path}")
    proc = run_tool(
        ["yq", "-o=json", "-I=0", "."],
        input=path.read_text(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: could not parse {path}: {proc.stderr.strip()}")
    doc = json.loads(proc.stdout or "null")
    return doc if isinstance(doc, dict) else {}


def plugin_default_bridge_port() -> int:
    doc = load_yaml(repo_root() / "plugins" / "browser" / "plugin.yml")
    hp = doc.get("host_port")
    return hp if isinstance(hp, int) else BRIDGE_PORT_DEFAULT


def read_secret(path: Path, var_name: str) -> str:
    """Read one variable out of secrets.env the way every other consumer does.

    up.sh SOURCES this file (`. "$SECRETS_FILE"`), so quoting, `export ` and
    escapes follow shell rules. Parsing it as text instead would disagree with
    the value the container was wired with — e.g. VAR="abc" would yield the
    quotes too, and the bridge would then reject every request the container
    made with the unquoted key. So source it in a shell and print the value.
    """
    if not path.is_file():
        return ""
    proc = run_tool(
        ["bash", "-c", '. "$1" >/dev/null 2>&1 || true; printf %s "${!2-}"',
         "_", str(path), var_name],
        capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def ensure_api_key(secrets_file: Path, var_name: str) -> str:
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    if not secrets_file.is_file():
        secrets_file.touch()
        secrets_file.chmod(0o600)
    existing = read_secret(secrets_file, var_name)
    if existing:
        return existing
    key = secrets.token_hex(24)
    with secrets_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{var_name}={key}\n")
    secrets_file.chmod(0o600)
    print(f"Generated {var_name} in {secrets_file}")
    return key


def browser_apps(env: dict | None = None) -> dict[str, Path]:
    """Where each browser lives, BRAVE_APP / CHROME_APP overriding the default."""
    e = os.environ if env is None else env
    return {
        "brave": Path(e.get("BRAVE_APP") or BRAVE_APP_DEFAULT),
        "chrome": Path(e.get("CHROME_APP") or CHROME_APP_DEFAULT),
    }


def pick_browser(choice: str, env: dict | None = None) -> Path:
    apps = browser_apps(env)
    if choice in apps:
        app = apps[choice]
    elif choice == "auto":
        app = apps["brave"] if apps["brave"].is_dir() else apps["chrome"]
    elif choice.startswith("/"):
        app = Path(choice)          # one-off: an explicit app bundle path
    else:
        raise SystemExit(
            "Usage: launch.py <container> [brave|chrome|/path/to/Browser.app]")
    if not app.is_dir():
        raise SystemExit(
            f"ERROR: browser not found at {app} "
            "(set BRAVE_APP / CHROME_APP in ./.env to point elsewhere)")
    return app


def cdp_ready(port: int) -> bool:
    proc = run_tool(
        ["curl", "-s", "-m", "2", f"http://127.0.0.1:{port}/json/version"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def launch_browser(app: Path, profile: Path, port: int) -> None:
    print(f"Launching {app.stem} with isolated profile {profile}")
    profile.mkdir(parents=True, exist_ok=True)
    run_tool(
        [
            "open",
            "-n",
            str(app),
            "--args",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        check=True,
    )
    for _ in range(20):
        if cdp_ready(port):
            return
        time.sleep(1)
    raise SystemExit("ERROR: browser CDP port never came up")


def build_bridge_command(bridge_port: int, cdp: int, api_key: str, tmp_dir: Path,
                         base_env: dict | None = None):
    """The (argv, env) the bridge is exec'd with.

    Split out from main() so the part that actually matters — TMPDIR reaching
    the chrome-devtools-mcp child — is assertable without a Mac, a browser, or
    a network.
    """
    env = dict(os.environ if base_env is None else base_env)
    # THE scoping mechanism: chrome-devtools-mcp restricts file-writing tools to
    # os.tmpdir() (plus client-negotiated MCP roots, which mcp-proxy does not
    # negotiate). Pointing TMPDIR at this container's exchange dir — bind-mounted
    # to /artifacts/browser inside the container — makes that dir, and only it,
    # writable. Descendants are allowed, so nested output paths work.
    env["TMPDIR"] = str(tmp_dir)
    argv = [
        "npx", "-y", "mcp-proxy",
        "--host", "127.0.0.1",
        "--port", str(bridge_port),
        "--server", "stream",
        "--apiKey", api_key,
        "--",
        "npx", "-y", "chrome-devtools-mcp",
        "--browserUrl", f"http://127.0.0.1:{cdp}",
    ]
    return argv, env


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        raise SystemExit("Usage: launch.py <container> [brave|chrome]")

    container = args[0]
    # Same charset rule service.sh applies to the plugin name, for the same
    # reason: the container name is interpolated into filesystem paths below
    # (manifest, profile dir, TMPDIR), so a slash or '..' must not escape.
    if not CONTAINER_NAME_RE.match(container):
        raise SystemExit(
            f"ERROR: invalid container name '{container}' "
            "(allowed: letters, digits, underscore, dash)")
    browser_choice = args[1] if len(args) > 1 else "auto"

    base_path = os.environ.get("BASE_PATH")
    if not base_path:
        raise SystemExit(
            "ERROR: BASE_PATH not set — run via ./service.sh browser <container>")

    manifest_path = bottles_dir(
        base_path, os.environ.get("BOTTLES_PATH")) / f"{container}.yml"
    manifest = load_yaml(manifest_path)
    bridge_port = validate_bridge_port(
        resolve_bridge_port(manifest, plugin_default_bridge_port()))
    cdp = cdp_port(bridge_port)
    profile = profile_dir(base_path, container)
    tmp_dir = browser_tmp_dir(base_path, container)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    secrets_file = Path(base_path) / "secrets.env"
    api_key = ensure_api_key(secrets_file, api_key_var(container, manifest))

    app = pick_browser(browser_choice)
    if not cdp_ready(cdp):
        launch_browser(app, profile, cdp)

    print(f"Bridging chrome-devtools-mcp on 127.0.0.1:{bridge_port} "
          "(X-API-Key required)")
    argv_exec, env = build_bridge_command(bridge_port, cdp, api_key, tmp_dir)
    # execvpe, not execve: "npx" is a PATH lookup, and execve would treat it as
    # a literal path and fail with ENOENT.
    try:
        os.execvpe(argv_exec[0], argv_exec, env)
    except FileNotFoundError:
        raise SystemExit(f"ERROR: required command not found on PATH: {argv_exec[0]}")


if __name__ == "__main__":
    main()
