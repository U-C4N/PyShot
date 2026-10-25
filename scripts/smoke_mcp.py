"""Protocol-only smoke test of a packaged executable; does not capture the desktop."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    executable = str(Path(sys.argv[1]).resolve())
    process = subprocess.Popen([executable, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "pyshot-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    # SDK exits cleanly on EOF, but retain input until both responses arrive.
    responses = [json.loads(process.stdout.readline()) for _ in range(2)]
    process.stdin.close()
    process.wait(timeout=20)
    errors = process.stderr.read()
    assert process.returncode == 0, errors
    by_id = {response["id"]: response for response in responses}
    assert "serverInfo" in by_id[1]["result"], by_id
    tools = by_id[2]["result"]["tools"]
    assert len(tools) == 9, tools
    print(f"Packaged MCP: initialize + {len(tools)} tools OK")


if __name__ == "__main__":
    main()
