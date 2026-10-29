"""Opt-in installed-client interoperability smoke, without persistent MCP changes.

Run with Python 3.13 from the development environment. By default this tests
setup/idempotency and Claude's actual MCP handshake using isolated configs.
Add --tool-calls only after starting the PyShot broker: it uses existing client
authentication and per-run configuration for three read-only calls, with no
permission bypass. JSON reports and CLI transcripts remain in --output-dir.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pyshot_core.setup_cli import _cli_prefix  # noqa: E402

READ_TOOLS = ["get_capabilities", "list_displays", "recording_status"]
PROMPT = (
    "This is a PyShot MCP interoperability smoke test. If PyShot tools are deferred, "
    "first use the built-in tool discovery/search facility to find tools from the "
    "pyshot MCP server. Discovery is permitted. Do not conclude the tools are "
    "unavailable without attempting discovery. Then call exactly these three "
    "PyShot tools once each with empty arguments: get_capabilities, list_displays, "
    "recording_status. Apart from discovery and those calls, do not perform any "
    "other action, capture an image, or start/stop "
    "recording. Then briefly report the three results. If tools or authentication "
    "are unavailable, report the exact failure; do not find workarounds."
)


def run(argv, env, cwd, output, label, timeout=90):
    """Store only this test command's outputs; never store auth status output."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", creationflags=flags)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            # Node launchers spawn the native CLI. Terminate only this test's
            # process tree so a timed-out subprocess cannot keep running.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False, creationflags=flags)
        else:
            process.kill()
        stdout, stderr = process.communicate(timeout=15)
    (output / f"{label}.stdout.jsonl").write_text(stdout, encoding="utf-8")
    (output / f"{label}.stderr.log").write_text(stderr, encoding="utf-8")
    return {"exit_code": process.returncode, "timed_out": timed_out,
            "stdout": stdout, "stderr": stderr}


def stage_result(result):
    return {key: result[key] for key in ("exit_code", "timed_out")}


def tool_evidence(client, stdout):
    attempted, succeeded, tool_ids = set(), set(), {}
    final = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if client == "codex":
            item = event.get("item", {})
            if item.get("type") == "mcp_tool_call" and item.get("server") == "pyshot":
                name = item.get("tool")
                attempted.add(name)
                result = item.get("result") or {}
                if (item.get("status") == "completed" and not item.get("error")
                        and not result.get("isError", result.get("is_error", False))):
                    succeeded.add(name)
            if item.get("type") == "agent_message":
                final = item.get("text", "")
            if event.get("type") == "error":
                final = event.get("message", "")
        else:
            for block in event.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name.startswith("mcp__pyshot__"):
                        name = name.removeprefix("mcp__pyshot__")
                        attempted.add(name)
                        tool_ids[block.get("id")] = name
                elif block.get("type") == "tool_result" and not block.get("is_error"):
                    name = tool_ids.get(block.get("tool_use_id"))
                    if name:
                        succeeded.add(name)
            if event.get("type") == "result":
                final = event.get("result", "") or "; ".join(event.get("errors", []))
    return {"attempted": sorted(attempted), "succeeded": sorted(succeeded),
            "passed": set(READ_TOOLS) <= succeeded, "final_message": final[:4000]}


def server_launch(path):
    if path is None:
        bundle = ROOT / "dist/PyShot-3.0.1-win-x64/pyshot-mcp.exe"
        path = bundle if bundle.is_file() else ROOT / "pyshot_mcp.py"
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return [str(path)] if path.suffix.lower() == ".exe" else [sys.executable, str(path)]


def probe_tools(prefixes, launch, output, env, timeout):
    # This preflight never autostarts the GUI. Tool calls are run only when the
    # caller has explicitly started the broker for the smoke test.
    from pyshot_core.rpc import BrokerClient
    try:
        with BrokerClient(autostart=False) as broker:
            broker.call("recording_status")
    except Exception as exc:
        return {"blocked": f"Broker preflight failed: {exc}"}

    work = output / "empty-workspace"
    work.mkdir(exist_ok=True)
    result = {}
    command, args = launch[0], [*launch[1:], "serve"]
    if "codex" in prefixes:
        codex_env = dict(env)
        # Preserve the real auth location; --ignore-user-config excludes its
        # settings/MCP entries, and --ephemeral avoids a saved conversation.
        codex_env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        overrides = {
            "mcp_servers.pyshot.command": command,
            "mcp_servers.pyshot.args": args,
            "mcp_servers.pyshot.enabled_tools": READ_TOOLS,
            "mcp_servers.pyshot.required": True,
            "mcp_servers.pyshot.startup_timeout_sec": 30,
            "features.shell_tool": False,
            "features.apps": False,
            "web_search": "disabled",
        }
        argv = [*prefixes["codex"], "exec", "--ignore-user-config", "--ephemeral",
                "--sandbox", "read-only", "--skip-git-repo-check", "--json"]
        for key, value in overrides.items():
            argv += ["-c", key + "=" + json.dumps(value)]
        raw = run([*argv, PROMPT], codex_env, work, output, "codex-tools", timeout)
        result["codex"] = {**stage_result(raw), **tool_evidence("codex", raw["stdout"]),
                           "stderr_tail": raw["stderr"][-3000:]}
    if "claude" in prefixes:
        config = output / "claude-run-mcp.json"
        config.write_text(json.dumps({"mcpServers": {"pyshot": {
            "type": "stdio", "command": command, "args": args}}}), encoding="utf-8")
        allowed = [f"mcp__pyshot__{tool}" for tool in READ_TOOLS]
        denied = [f"mcp__pyshot__{tool}" for tool in (
            "list_windows", "list_audio_devices", "capture_screenshot", "recording_start",
            "recording_stop", "recording_frames")]
        argv = [*prefixes["claude"], "--print", "--no-session-persistence", "--restricted",
                "--strict-mcp-config", "--mcp-config", str(config), "--tools", "",
                "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied),
                "--permission-prompts", "none", "--disable-slash-commands",
                "--output-format", "stream-json", "--verbose", PROMPT]
        raw = run(argv, env, work, output, "claude-tools", timeout)
        result["claude"] = {**stage_result(raw), **tool_evidence("claude", raw["stdout"]),
                            "stderr_tail": raw["stderr"][-3000:]}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, help="Bundle EXE or source entry; autodetected by default")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--tool-calls", action="store_true", help="Use existing auth; requires a running broker")
    parser.add_argument("--clients", default="codex,claude", help="Comma-separated clients to test")
    parser.add_argument("--timeout", type=int, default=180, help="Per-client tool-call timeout")
    options = parser.parse_args(argv)
    selected = {name.strip() for name in options.clients.split(",") if name.strip()}
    if not selected or selected - {"codex", "claude"}:
        parser.error("--clients must name codex, claude, or both")
    if options.output_dir:
        output = options.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
    else:
        parent = ROOT / "build" / "client-smoke"
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    launch = server_launch(options.server)
    env = dict(os.environ)
    isolated = dict(env)
    isolated["CODEX_HOME"] = str(output / "codex-config")
    isolated["CLAUDE_CONFIG_DIR"] = str(output / "claude-config")
    Path(isolated["CODEX_HOME"]).mkdir(exist_ok=True)
    Path(isolated["CLAUDE_CONFIG_DIR"]).mkdir(exist_ok=True)
    prefixes = {name: _cli_prefix(name, found, shutil.which)
                for name in ("codex", "claude") if name in selected and (found := shutil.which(name))}
    report = {"server": launch, "output_dir": str(output), "installed_clients": sorted(prefixes),
              "missing_clients": sorted(selected - prefixes.keys())}
    if not prefixes:
        report["passed"] = False
    else:
        clients = ",".join(prefixes)
        setup = []
        for index in (1, 2):
            raw = run([*launch, "setup", "--clients", clients], isolated, output,
                      output, f"setup-{index}")
            try:
                entries = json.loads(raw["stdout"])["clients"]
            except (ValueError, KeyError):
                entries = []
            expected = "configured" if index == 1 else "unchanged"
            setup.append({**stage_result(raw), "clients": entries,
                          "passed": bool(entries) and all(x["status"] == expected for x in entries)})
        report["setup"] = setup
        health = {}
        for client, prefix in prefixes.items():
            argv = [*prefix, "mcp", "get", "pyshot"] + (["--json"] if client == "codex" else [])
            raw = run(argv, isolated, output, output, client + "-health")
            health[client] = {**stage_result(raw), "output": raw["stdout"][:4000],
                              "stderr": raw["stderr"][-2000:],
                              "check": "registration" if client == "codex" else "MCP handshake"}
            health[client]["passed"] = raw["exit_code"] == 0 and (
                client == "codex" or any("Status:" in line and line.strip().endswith(" Connected")
                                         for line in raw["stdout"].splitlines()))
        report["health"] = health
        if options.tool_calls:
            report["tool_calls"] = probe_tools(prefixes, launch, output, env, options.timeout)
        report["passed"] = all(item["passed"] for item in setup) and all(
            item["passed"] for item in health.values())
        if options.tool_calls:
            report["passed"] &= all(report["tool_calls"].get(client, {}).get("passed", False)
                                    for client in prefixes)
        report["passed"] &= not report["missing_clients"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows legacy console encodings cannot render health-check glyphs.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
