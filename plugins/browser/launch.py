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

BRAVE = Path("/Applications/Brave Browser.app")
CHROME = Path("/Applications/Google Chrome.app")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def containers_dir(base_path: str, env_override: str | None = None) -> Path:
    """Mirror src/common.sh CONTAINERS_PATH resolution."""
    if env_override:
        return Path(env_override)
    custom = Path(base_path) / "containers"
    if custom.is_dir():
        return custom
    return repo_root() / "containers"


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


def profile_dir(base_path: str, container: str) -> Path:
    return Path(base_path) / "browser-profiles" / container


def browser_tmp_dir(base_path: str, container: str) -> Path:
    """Host TMPDIR for chrome-devtools-mcp; bind-mounted at /artifacts/browser."""
    return Path(base_path) / "browser-tmp" / container


def api_key_var(container: str) -> str:
    return f"RESEARCH_BROWSER_KEY_{container.replace('-', '_')}"


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"ERROR: file not found: {path}")
    proc = subprocess.run(
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


def parse_secrets_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def ensure_api_key(secrets_file: Path, var_name: str) -> str:
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    if not secrets_file.is_file():
        secrets_file.touch()
        secrets_file.chmod(0o600)
    data = parse_secrets_file(secrets_file)
    if data.get(var_name):
        return data[var_name]
    key = secrets.token_hex(24)
    with secrets_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{var_name}={key}\n")
    secrets_file.chmod(0o600)
    print(f"Generated {var_name} in {secrets_file}")
    return key


def pick_browser(choice: str) -> Path:
    if choice == "brave":
        app = BRAVE
    elif choice == "chrome":
        app = CHROME
    elif choice == "auto":
        app = BRAVE if BRAVE.is_dir() else CHROME
    else:
        raise SystemExit("Usage: launch.py <container> [brave|chrome]")
    if not app.is_dir():
        raise SystemExit(f"ERROR: browser not found at {app}")
    return app


def cdp_ready(port: int) -> bool:
    try:
        proc = subprocess.run(
            ["curl", "-s", "-m", "2", f"http://127.0.0.1:{port}/json/version"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def launch_browser(app: Path, profile: Path, port: int) -> None:
    print(f"Launching {app.stem} with isolated profile {profile}")
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
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
        raise SystemExit("ERROR: BASE_PATH not set — run via ./service.sh browser")

    manifest_path = containers_dir(base_path, os.environ.get("CONTAINERS_PATH")) / f"{container}.yml"
    manifest = load_yaml(manifest_path)
    bridge_port = resolve_bridge_port(manifest, plugin_default_bridge_port())
    cdp = cdp_port(bridge_port)
    profile = profile_dir(base_path, container)
    tmp_dir = browser_tmp_dir(base_path, container)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    secrets_file = Path(base_path) / "secrets.env"
    api_key = ensure_api_key(secrets_file, api_key_var(container))

    app = pick_browser(browser_choice)
    if not cdp_ready(cdp):
        launch_browser(app, profile, cdp)

    print(f"Bridging chrome-devtools-mcp on 127.0.0.1:{bridge_port} (X-API-Key required)")
    env = os.environ.copy()
    # THE scoping mechanism: chrome-devtools-mcp restricts file-writing tools to
    # os.tmpdir() (plus client-negotiated MCP roots, which mcp-proxy does not
    # negotiate). Pointing TMPDIR at this container's exchange dir — bind-mounted
    # to /artifacts/browser inside the container — makes that dir, and only it,
    # writable. Descendants are allowed, so nested output paths work.
    env["TMPDIR"] = str(tmp_dir)
    # execvpe, not execve: "npx" is a PATH lookup, and execve would treat it as
    # a literal path and fail with ENOENT.
    os.execvpe(
        "npx",
        [
            "npx",
            "-y",
            "mcp-proxy",
            "--host",
            "127.0.0.1",
            "--port",
            str(bridge_port),
            "--server",
            "stream",
            "--apiKey",
            api_key,
            "--",
            "npx",
            "-y",
            "chrome-devtools-mcp",
            "--browserUrl",
            f"http://127.0.0.1:{cdp}",
        ],
        env,
    )


if __name__ == "__main__":
    main()
