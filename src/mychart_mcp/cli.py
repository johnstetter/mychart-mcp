"""Command line: `mychart-mcp import|stats|serve|client-config`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .db import connect_rw, db_path


def _import(args: argparse.Namespace) -> int:
    zip_path = Path(args.export).expanduser()
    if not zip_path.is_file():
        print(f"Not found: {zip_path}", file=sys.stderr)
        return 1
    from .importers import import_apple_health

    conn = connect_rw()
    try:
        counts = import_apple_health(conn, zip_path)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()

    total = sum(n for k, n in counts.items() if not k.startswith("_"))
    print(f"Imported {total} records into {db_path()}")
    for rtype, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {rtype:<22} {n}")
    print(
        f"\nThe export still contains your full health data: {zip_path}\n"
        "Delete it (or move it somewhere encrypted) once you're happy with the import."
    )
    return 0


def _stats(args: argparse.Namespace) -> int:
    from mcp.server.mcpserver.exceptions import ToolError

    from .server import data_summary

    try:
        summary = data_summary()
    except ToolError as e:
        print(e, file=sys.stderr)
        return 1
    for row in summary["record_types"]:
        span = f"{row['first'] or '?'} – {row['last'] or '?'}"
        print(f"{row['resource_type']:<22} {row['n']:>6}  {span}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import run

    run()
    return 0


def _server_entry() -> dict:
    # Desktop apps launch servers with a minimal PATH and an arbitrary working directory,
    # so point them at this environment's executable by absolute path.
    entry: dict = {"command": str(Path(sys.executable).parent / "mychart-mcp"), "args": ["serve"]}
    if "MYCHART_MCP_DB" in os.environ:
        entry["env"] = {"MYCHART_MCP_DB": str(db_path())}
    return entry


def _client_config(args: argparse.Namespace) -> int:
    entry = _server_entry()
    if args.client == "claude":
        print("# Merge into ~/Library/Application Support/Claude/claude_desktop_config.json")
        print(json.dumps({"mcpServers": {"mychart": entry}}, indent=2))
    else:
        # JSON scalars and flow sequences are valid YAML, which keeps quoting safe.
        print("# Add under mcp_servers: in ~/.hermes/config.yaml")
        print("mcp_servers:\n  mychart:")
        for key, value in entry.items():
            if isinstance(value, dict):
                print(f"    {key}:")
                for k, v in value.items():
                    print(f"      {k}: {json.dumps(v)}")
            else:
                print(f"    {key}: {json.dumps(value)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="mychart-mcp")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("import", help="Import an Apple Health export.zip")
    p.add_argument("export", help="Path to export.zip from the Health app")
    p.set_defaults(func=_import)

    sub.add_parser("serve", help="Run the MCP server on stdio").set_defaults(func=_serve)
    sub.add_parser("stats", help="Show what's in the database").set_defaults(func=_stats)

    p = sub.add_parser("client-config", help="Print config for Claude Desktop or Hermes")
    p.add_argument("client", choices=["claude", "hermes"])
    p.set_defaults(func=_client_config)

    args = parser.parse_args()
    sys.exit(args.func(args))
