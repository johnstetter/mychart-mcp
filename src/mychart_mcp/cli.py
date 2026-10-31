"""Command line: `mychart-mcp import|sync|stats|serve|client-config`."""

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


def _sync(args: argparse.Namespace) -> int:
    import webbrowser

    from . import epic

    fhir_base = epic.EPIC_SANDBOX if args.sandbox else args.fhir_base
    if not fhir_base:
        print(
            "Pass --fhir-base (your health system's Epic R4 endpoint) or --sandbox.",
            file=sys.stderr,
        )
        return 1
    if not args.client_id:
        print("Pass --client-id or set MYCHART_MCP_CLIENT_ID.", file=sys.stderr)
        return 1
    if args.sandbox and "MYCHART_MCP_DB" not in os.environ:
        print(
            "Set MYCHART_MCP_DB to a scratch path for --sandbox so test patients don't mix "
            "with your records.",
            file=sys.stderr,
        )
        return 1

    def open_url(url: str) -> None:
        print(f"Opening your browser to sign in. If it doesn't open, visit:\n  {url}\n", flush=True)
        webbrowser.open(url)

    try:
        token, patient_id = epic.login(
            fhir_base,
            args.client_id,
            args.redirect_uri,
            cert=args.cert,
            key=args.key,
            open_url=open_url,
        )
        print("Signed in. Downloading records…", flush=True)
        resources, problems = epic.fetch_chart(epic.Http(fhir_base, token), patient_id)
    except (epic.FhirError, OSError) as e:
        print(e, file=sys.stderr)
        return 1

    source = args.name or epic.source_name(resources[0], fhir_base)
    conn = connect_rw()
    try:
        counts = epic.store(conn, source, fhir_base, resources)
    finally:
        conn.close()

    total = sum(n for k, n in counts.items() if not k.startswith("_"))
    print(f"Synced {total} records from {source} into {db_path()}")
    for rtype, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {rtype:<22} {n}")
    if problems:
        print("\nSkipped (usually an API not enabled for the app, or not offered by this server):")
        for problem, n in problems.items():
            print(f"  {problem}" + (f" ×{n}" if n > 1 else ""))
    return 0


def _forget(args: argparse.Namespace) -> int:
    from .importers import delete_records

    conn = connect_rw()
    try:
        where = "origin = ?" + (" AND source = ?" if args.source is not None else "")
        params = (args.origin,) if args.source is None else (args.origin, args.source)
        n = conn.execute(f"SELECT COUNT(*) FROM resources WHERE {where}", params).fetchone()[0]
        what = args.origin + (f" ({args.source})" if args.source is not None else "")
        if n == 0:
            print(f"No {what} records in {db_path()}")
            return 0
        if not args.yes and input(f"Delete {n} {what} records from {db_path()}? [y/N] ") != "y":
            print("Nothing deleted.")
            return 1
        with conn:
            delete_records(conn, args.origin, args.source)
            if args.source is None:
                conn.execute("DELETE FROM import_meta WHERE origin = ?", (args.origin,))
        # Deleted rows linger in free pages until the file is rebuilt.
        conn.execute("VACUUM")
    finally:
        conn.close()
    print(f"Deleted {n} {what} records.")
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
    print()
    for imp in summary["imports"]:
        print(f"Imported via {imp['origin']} at {imp['imported_at']}")
    if summary["sources"]:
        print("Sources: " + ", ".join(summary["sources"]))
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

    p = sub.add_parser("sync", help="Download records directly from an Epic FHIR server")
    p.add_argument("--fhir-base", default=os.environ.get("MYCHART_MCP_FHIR_BASE"))
    p.add_argument("--sandbox", action="store_true", help="Use Epic's public sandbox")
    p.add_argument("--client-id", default=os.environ.get("MYCHART_MCP_CLIENT_ID"))
    p.add_argument(
        "--redirect-uri",
        default=os.environ.get("MYCHART_MCP_REDIRECT_URI", "http://127.0.0.1:8765/callback"),
    )
    p.add_argument("--cert", default=os.environ.get("MYCHART_MCP_CERT"), help="for https redirect")
    p.add_argument("--key", default=os.environ.get("MYCHART_MCP_KEY"), help="for https redirect")
    p.add_argument("--name", help="Source name for these records (default: from the chart)")
    p.set_defaults(func=_sync)

    p = sub.add_parser("forget", help="Delete every record from one import source")
    p.add_argument("origin", choices=["apple_health", "fhir"])
    p.add_argument("--source", help="Only this health system (as listed by stats)")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    p.set_defaults(func=_forget)

    sub.add_parser("serve", help="Run the MCP server on stdio").set_defaults(func=_serve)
    sub.add_parser("stats", help="Show what's in the database").set_defaults(func=_stats)

    p = sub.add_parser("client-config", help="Print config for Claude Desktop or Hermes")
    p.add_argument("client", choices=["claude", "hermes"])
    p.set_defaults(func=_client_config)

    args = parser.parse_args()
    sys.exit(args.func(args))
