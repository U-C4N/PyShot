"""Client setup tests use temporary homes and a fake official CLI exclusively."""
import json
from pathlib import Path
import subprocess

import pytest

from pyshot_core import setup_cli


def write_entry(home, client, entry):
    if client == "claude":
        path = home / ".claude.json"
        config = json.loads(path.read_text()) if path.exists() else {"unrelated": "keep"}
        entries = config.setdefault("mcpServers", {})
        if entry is None:
            entries.pop("pyshot", None)
        else:
            entries["pyshot"] = entry
        path.write_text(json.dumps(config), encoding="utf-8")
    else:
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(exist_ok=True)
        text = 'model = "keep"\n'
        if entry is not None:
            text += "[mcp_servers.pyshot]\n"
            for key, value in entry.items():
                if key != "env":
                    text += f"{key} = {json.dumps(value)}\n"
            if entry.get("env"):
                text += "[mcp_servers.pyshot.env]\n"
                text += "".join(f"{key} = {json.dumps(value)}\n"
                                for key, value in entry["env"].items())
        path.write_text(text, encoding="utf-8")


class FakeCli:
    def __init__(self, home):
        self.home = home
        self.calls = []
        self.fail_next_add = False
        self.skip_save = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs.get("shell") is None
        assert kwargs["timeout"] == 30
        client, _, action = argv[:3]
        if action == "remove":
            write_entry(self.home, client, None)
        elif action == "add":
            if self.fail_next_add:
                self.fail_next_add = False
                return subprocess.CompletedProcess(argv, 1, "", "failure")
            split = argv.index("--")
            entry = {"command": argv[split + 1], "args": argv[split + 2:]}
            env = {}
            for index, value in enumerate(argv[:split]):
                if value == "--env":
                    key, data = argv[index + 1].split("=", 1)
                    env[key] = data
            if env:
                entry["env"] = env
            if not self.skip_save:
                write_entry(self.home, client, entry)
        return subprocess.CompletedProcess(argv, 0, "Added", "")


def run_setup(home, fake, clients=("codex", "claude"), **kwargs):
    return setup_cli.setup_clients(clients, command=r"C:\Program Files\PyShot\pyshot-mcp.exe",
                                  args=["serve"], home=home, environ={}, runner=fake,
                                  which=lambda name: name, **kwargs)


def test_registers_using_official_commands_and_is_idempotent(tmp_path):
    cli = FakeCli(tmp_path)
    assert [x["status"] for x in run_setup(tmp_path, cli)] == ["configured", "configured"]
    assert cli.calls == [
        ["codex", "mcp", "add", "pyshot", "--", r"C:\Program Files\PyShot\pyshot-mcp.exe", "serve"],
        ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", "pyshot", "--",
         r"C:\Program Files\PyShot\pyshot-mcp.exe", "serve"],
    ]
    assert [x["status"] for x in run_setup(tmp_path, cli)] == ["unchanged", "unchanged"]
    assert len(cli.calls) == 2
    assert json.loads((tmp_path / ".claude.json").read_text())["unrelated"] == "keep"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_unrelated_name_conflict_is_not_removed(tmp_path, client):
    write_entry(tmp_path, client, {"command": "another-server", "args": []})
    cli = FakeCli(tmp_path)
    result = run_setup(tmp_path, cli, [client])[0]
    assert result["status"] == "error"
    assert "CONFIG_NAME_CONFLICT" in result["message"]
    assert cli.calls == []


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_old_pyshot_path_is_updated_and_environment_preserved(tmp_path, client):
    write_entry(tmp_path, client, {"command": r"C:\Old\pyshot-mcp.exe", "args": ["serve"],
                                   "env": {"PYSHOT_TEST": "value with spaces"}})
    cli = FakeCli(tmp_path)
    result = run_setup(tmp_path, cli, [client])[0]
    assert result["status"] == "updated"
    assert cli.calls[0] == [client, "mcp", "remove", "pyshot"] + (
        ["--scope", "user"] if client == "claude" else [])
    assert "PYSHOT_TEST=value with spaces" in cli.calls[1]


def test_failed_update_restores_old_registration(tmp_path):
    original = {"command": r"C:\Old\pyshot-mcp.exe", "args": ["serve"]}
    write_entry(tmp_path, "claude", original)
    cli = FakeCli(tmp_path)
    cli.fail_next_add = True
    result = run_setup(tmp_path, cli, ["claude"])[0]
    assert result["status"] == "error"
    assert "previous registration restored" in result["message"]
    assert setup_cli._read_entry("claude", tmp_path / ".claude.json") == original


def test_success_exit_without_saved_configuration_is_error(tmp_path):
    cli = FakeCli(tmp_path)
    cli.skip_save = True
    result = run_setup(tmp_path, cli, ["claude"])[0]
    assert result["status"] == "error"
    assert "did not save" in result["message"]


def test_invalid_config_is_not_overwritten(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{broken", encoding="utf-8")
    cli = FakeCli(tmp_path)
    assert run_setup(tmp_path, cli, ["claude"])[0]["status"] == "error"
    assert path.read_text() == "{broken"
    assert cli.calls == []


def test_customized_owned_registration_is_not_rewritten(tmp_path):
    write_entry(tmp_path, "codex", {"command": r"C:\Old\pyshot-mcp.exe", "args": ["serve"],
                                    "startup_timeout_sec": 90})
    cli = FakeCli(tmp_path)
    result = run_setup(tmp_path, cli, ["codex"])[0]
    assert "CONFIG_CUSTOMIZED" in result["message"]
    assert cli.calls == []


def test_missing_cli_and_unknown_client(tmp_path):
    result = setup_cli.setup_clients(["claude"], home=tmp_path, environ={}, which=lambda _: None)
    assert result[0]["status"] == "missing_cli"
    with pytest.raises(ValueError, match="Unknown clients"):
        setup_cli.setup_clients(["browser"])


def test_config_root_overrides_are_used(tmp_path):
    assert setup_cli._config_path("codex", tmp_path, {"CODEX_HOME": "x"}) == Path("x/config.toml")
    assert setup_cli._config_path("claude", tmp_path, {"CLAUDE_CONFIG_DIR": "y"}) == Path("y/.claude.json")


@pytest.mark.parametrize("client,entry", [
    ("codex", "@openai/codex/bin/codex.js"),
    ("claude", "@anthropic-ai/claude-code/cli.js"),
])
def test_npm_batch_shims_use_node_without_shell(tmp_path, client, entry):
    script = tmp_path / "node_modules" / entry
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    prefix = setup_cli._cli_prefix(client, str(tmp_path / f"{client}.cmd"), lambda _: "node.exe")
    assert prefix == ["node.exe", str(script)]
    command = setup_cli._add_command(client, prefix, r"C:\A&B\pyshot-mcp.exe", ["serve"],
                                     {"CUSTOM": "value&with%metacharacters!"})
    assert command[:2] == prefix
    assert "CUSTOM=value&with%metacharacters!" in command
    assert command[-2] == r"C:\A&B\pyshot-mcp.exe"


def test_unknown_batch_launcher_is_not_executed(tmp_path):
    with pytest.raises(ValueError, match="Cannot resolve"):
        setup_cli._cli_prefix("codex", str(tmp_path / "codex.cmd"), lambda _: "node.exe")


def test_timeout_does_not_expose_configured_secrets(tmp_path):
    def fail(argv, **kwargs):
        raise subprocess.TimeoutExpired([*argv, "SENSITIVE_CONFIG_VALUE"], 30)
    result = run_setup(tmp_path, fail, ["claude"])[0]
    assert result["status"] == "error"
    assert "SENSITIVE_CONFIG_VALUE" not in result["message"]
