"""Console entry point: MCP stdio by default, or explicit client setup."""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PyShot local MCP server for Codex CLI and Claude Code")
    subcommands = parser.add_subparsers(dest="mode")
    subcommands.add_parser("serve", help="Serve MCP over stdin/stdout (the default)")
    setup = subcommands.add_parser("setup", help="Register PyShot in the selected CLI clients")
    setup.add_argument("--clients", default="codex,claude", help="Comma-separated: codex,claude")
    options = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        if options.mode == "setup":
            from pyshot_core.setup_cli import setup_clients
            clients = [item.strip() for item in options.clients.split(",") if item.strip()]
            if not clients:
                parser.error("--clients must name codex, claude, or both")
            results = setup_clients(clients)
            print(json.dumps({"clients": results}, ensure_ascii=False, indent=2))
            return int(any(item["status"] in {"error", "missing_cli"} for item in results))
        from pyshot_core.mcp_server import serve
        serve()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # No import of the GUI's dependency guard: CLI errors never open dialogs.
        print(f"PyShot MCP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
