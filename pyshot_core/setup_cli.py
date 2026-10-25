"""Register the local stdio server using the clients' supported CLI commands.

Configuration is read only to make setup idempotent and to avoid replacing an
unrelated server. All writes go through ``codex mcp`` or ``claude mcp``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


SERVER_NAME = "pyshot"


def launch_spec() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ["serve"]
    entry = Path(__file__).resolve().parents[1] / "pyshot_mcp.py"
    return str(Path(sys.executable).resolve()), [str(entry), "serve"]


def _config_path(client: str, home: Path, environ: dict) -> Path:
    if client == "codex":
        return Path(environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    # CLAUDE_CONFIG_DIR relocates Claude's user configuration, including its
    # .claude.json equivalent. The default remains the file in the home folder.
    directory = environ.get("CLAUDE_CONFIG_DIR")
    return Path(directory) / ".claude.json" if directory else home / ".claude.json"


def _read_entry(client: str, path: Path) -> dict | None:
    if not path.exists():
        return None
    if client == "codex":
        config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        entries = config.get("mcp_servers", {})
    else:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = config.get("mcpServers", {})
    if not isinstance(entries, dict):
        raise ValueError("MCP server configuration must be an object")
    entry = entries.get(SERVER_NAME)
    if entry is not None and not isinstance(entry, dict):
        raise ValueError("The existing pyshot configuration must be an object")
    return entry


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _same_launch(entry: dict, command: str, args: list[str]) -> bool:
    if entry.get("type", "stdio") != "stdio" or "url" in entry:
        return False
    return (_normal_path(str(entry.get("command", ""))) == _normal_path(command)
            and entry.get("args", []) == args)


def _is_ours(entry: dict) -> bool:
    if entry.get("type", "stdio") != "stdio" or "url" in entry:
        return False
    # Only a dedicated PyShot executable or the first Python script argument
    # counts. A coincidental mention of PyShot in other arguments is not enough.
    command = str(entry.get("command", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
    args = entry.get("args", [])
    return (command in {"pyshot-mcp.exe", "pyshot-mcp"}
            or (command in {"python", "python.exe", "python3", "python3.exe"}
                and isinstance(args, list) and bool(args)
                and str(args[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
                == "pyshot_mcp.py"))


def _cli_prefix(client: str, cli: str, which) -> list[str]:
    if Path(cli).suffix.lower() not in {".cmd", ".bat"}:
        return [cli]
    # Windows npm shims are batch files: CreateProcess can pass their arguments
    # through cmd.exe even with shell=False. Invoke the official Node entry
    # directly so paths/environment values containing shell metacharacters stay
    # literal. Native installers and executable binaries need no translation.
    package = ("@openai/codex/bin/codex.js" if client == "codex"
               else "@anthropic-ai/claude-code/cli.js")
    script = Path(cli).parent / "node_modules" / package
    bundled_node = Path(cli).parent / "node.exe"
    node = str(bundled_node) if bundled_node.is_file() else which("node")
    if not script.is_file() or not node or Path(node).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError(f"Cannot resolve the {client} npm launcher; use its native installer "
                         "or install Node.js and the official npm package")
    return [node, str(script)]


def _add_command(client: str, cli: list[str], command: str, args: list[str], env=None) -> list[str]:
    result = [*cli, "mcp", "add"]
    if client == "claude":
        result += ["--transport", "stdio", "--scope", "user"]
    result.append(SERVER_NAME)
    for key, value in (env or {}).items():
        result += ["--env", f"{key}={value}"]
    return result + ["--", command, *args]


def _remove_command(client: str, cli: list[str]) -> list[str]:
    result = [*cli, "mcp", "remove", SERVER_NAME]
    return result + (["--scope", "user"] if client == "claude" else [])


def _run(command: list[str], runner) -> subprocess.CompletedProcess:
    # Argument arrays avoid shell interpolation and preserve paths with spaces.
    kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=30, check=False)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return runner(command, **kwargs)


def _require_success(result, action: str) -> None:
    if result.returncode:
        # CLI output can contain configured environment values. Keep it out of
        # setup's error report rather than accidentally displaying credentials.
        raise RuntimeError(f"{action} failed (exit {result.returncode})")


def setup_clients(clients, *, command=None, args=None, home=None, environ=None,
                  runner=None, which=None) -> list[dict]:
    """Set up selected clients; injected dependencies keep tests off real config."""
    clients = list(dict.fromkeys(clients))
    unknown = set(clients) - {"codex", "claude"}
    if unknown:
        raise ValueError("Unknown clients: " + ", ".join(sorted(unknown)))
    if command is None:
        command, default_args = launch_spec()
        args = default_args if args is None else args
    args = list(args or [])
    command = str(command)
    home = Path.home() if home is None else Path(home)
    environ = dict(os.environ if environ is None else environ)
    runner = subprocess.run if runner is None else runner
    which = shutil.which if which is None else which
    results = []
    for client in clients:
        cli = which(client)
        if not cli:
            results.append({"client": client, "status": "missing_cli",
                            "message": f"{client} was not found on PATH"})
            continue
        entry = None
        removed = False
        try:
            cli = _cli_prefix(client, cli, which)
            entry = _read_entry(client, _config_path(client, home, environ))
            if entry is not None and _same_launch(entry, command, args):
                results.append({"client": client, "status": "unchanged"})
                continue
            if entry is not None:
                if not _is_ours(entry):
                    raise ValueError("CONFIG_NAME_CONFLICT: 'pyshot' belongs to another server")
                # The supported add flags cannot round-trip every client setting.
                # Refuse a destructive rewrite of a user's customized entry.
                if set(entry) - {"type", "command", "args", "env"}:
                    raise ValueError("CONFIG_CUSTOMIZED: existing server has custom settings; "
                                     "update its executable path with the client CLI")
                _require_success(_run(_remove_command(client, cli), runner), "Removing old registration")
                removed = True
            _require_success(_run(_add_command(client, cli, command, args,
                                               entry.get("env") if entry else None), runner),
                             "Registering PyShot")
            # Do not infer configuration success from a health check's exit code:
            # Claude can print a failed connection with a successful exit status.
            saved = _read_entry(client, _config_path(client, home, environ))
            if saved is None or not _same_launch(saved, command, args):
                raise RuntimeError("The client did not save the expected PyShot registration")
            results.append({"client": client, "status": "updated" if removed else "configured"})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            # TimeoutExpired/CalledProcessError include the argv, potentially
            # exposing preserved environment credentials in the setup report.
            message = "Client command failed or timed out" if isinstance(exc, subprocess.SubprocessError) else str(exc)
            if removed and entry is not None:
                try:
                    # A failed add can have written a partial entry. Remove only
                    # that same PyShot name before restoring its previous launch.
                    _run(_remove_command(client, cli), runner)
                    restored = _run(_add_command(client, cli, entry["command"],
                                                  entry.get("args", []), entry.get("env")), runner)
                    _require_success(restored, "Restoring previous registration")
                    saved = _read_entry(client, _config_path(client, home, environ))
                    if saved is None or not _same_launch(saved, entry["command"], entry.get("args", [])):
                        raise RuntimeError("Client did not restore the previous registration")
                    message += "; previous registration restored"
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    message += "; previous registration could not be restored"
            results.append({"client": client, "status": "error", "message": message})
    return results
