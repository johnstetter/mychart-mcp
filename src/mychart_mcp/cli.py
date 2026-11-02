"""CLI: import, sync, logout, forget, serve, stats, schedule-config, client-config."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .db import connect_ro, connect_rw, db_path


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


def _sync_sign_in(args: argparse.Namespace, fhir_base: str) -> tuple[str, str] | None:
    """Saved sign-in if there is one, else the browser. Returns (token, patient) or None."""
    import webbrowser

    from . import epic, keychain

    credential = None
    try:
        credential = keychain.load(fhir_base)
    except keychain.KeychainError as e:
        if args.unattended or args.remember:
            print(e, file=sys.stderr)
            return None
    if credential:
        try:
            token = epic.saved_login(credential)
            print("Using the sign-in saved in the Keychain.", flush=True)
            return token
        except epic.FhirError as e:
            print(e, file=sys.stderr)
            if args.unattended:
                return None
            print("Signing in with the browser instead.", flush=True)
    elif args.unattended:
        print(
            f"No saved sign-in for {fhir_base}. Run `mychart-mcp sync --remember` once "
            "interactively first.",
            file=sys.stderr,
        )
        return None

    if not args.client_id:
        print("Pass --client-id or set MYCHART_MCP_CLIENT_ID.", file=sys.stderr)
        return None
    for path in (args.cert, args.key):
        if path and not Path(path).expanduser().is_file():
            print(
                f"Certificate file not found: {Path(path).expanduser().absolute()}\n"
                "Create it with `mkcert 127.0.0.1` in that folder (after `mkcert -install`).",
                file=sys.stderr,
            )
            return None

    def open_url(url: str) -> None:
        print(f"Opening your browser to sign in. If it doesn't open, visit:\n  {url}\n", flush=True)
        webbrowser.open(url)

    login = epic.login(
        fhir_base,
        args.client_id,
        args.redirect_uri,
        cert=args.cert,
        key=args.key,
        open_url=open_url,
        offline=args.remember,
    )
    if args.remember:
        try:
            keychain.save(fhir_base, epic.register_client(login, args.client_id))
            print("Saved this sign-in to the Keychain for unattended syncs.", flush=True)
        except (epic.FhirError, keychain.KeychainError) as e:
            print(f"Couldn't save the sign-in ({e}); continuing with this sync.", file=sys.stderr)
    return login.access_token, login.patient_id


def _sync(args: argparse.Namespace) -> int:
    from . import epic

    fhir_base = epic.EPIC_SANDBOX if args.sandbox else args.fhir_base
    if not fhir_base:
        print(
            "Pass --fhir-base (your health system's Epic R4 endpoint) or --sandbox.",
            file=sys.stderr,
        )
        return 1
    if args.sandbox and "MYCHART_MCP_DB" not in os.environ:
        print(
            "Set MYCHART_MCP_DB to a scratch path for --sandbox so test patients don't mix "
            "with your records.",
            file=sys.stderr,
        )
        return 1

    try:
        signed_in = _sync_sign_in(args, fhir_base)
        if signed_in is None:
            return 1
        token, patient_id = signed_in
        http = epic.Http(fhir_base, token)
        patient = epic.fetch_patient(http, patient_id)
    except (epic.FhirError, OSError) as e:
        print(e, file=sys.stderr)
        return 1

    source = args.name or epic.source_name(patient, fhir_base)
    conn = connect_rw()
    try:
        since = epic.sync_since(conn, fhir_base, patient_id, source, full=args.full)
        changes_only = epic.changes_only_types(http) if since else frozenset()
        if since:
            print(
                f"Signed in. Downloading changes since {since[:10]}"
                f" ({', '.join(sorted(changes_only)) or 'none'}; the rest in full)…",
                flush=True,
            )
        else:
            print("Signed in. Downloading your whole chart (full sync)…", flush=True)
        started = datetime.now(UTC)
        try:
            chart = epic.fetch_chart(
                http,
                patient,
                since=since,
                progress=lambda s: print(s, flush=True),
                stored_note=lambda note_id: epic.stored_note(conn, source, note_id),
                changes_only=changes_only,
            )
        except (epic.FhirError, OSError) as e:
            print(e, file=sys.stderr)
            return 1
        counts = epic.store(conn, source, fhir_base, chart, started)
    finally:
        conn.close()

    total = sum(n for k, n in counts.items() if not k.startswith("_"))
    if since:
        print(f"Updated {source} in {db_path()}: {total} records new, changed or refreshed")
    else:
        print(f"Synced {total} records from {source} into {db_path()}")
    for rtype, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {rtype:<22} {n}")
    if chart.problems:
        print("\nSkipped (usually an API not enabled for the app, or not offered by this server):")
        for problem, n in chart.problems.items():
            print(f"  {problem}" + (f" ×{n}" if n > 1 else ""))
        again = "mychart-mcp sync --full" if chart.needs_full_sync else "mychart-mcp sync"
        print(f"If these were temporary (timeouts), run `{again}` again later.")
    return 0


def _logout(args: argparse.Namespace) -> int:
    from . import epic, keychain

    fhir_base = epic.EPIC_SANDBOX if args.sandbox else args.fhir_base
    if not fhir_base:
        print("Pass --fhir-base or --sandbox.", file=sys.stderr)
        return 1
    try:
        removed = keychain.delete(fhir_base)
    except keychain.KeychainError as e:
        print(e, file=sys.stderr)
        return 1
    print("Removed the saved sign-in." if removed else f"No saved sign-in for {fhir_base}.")
    print("To revoke it on the Epic side too, remove the app under Linked Apps in MyChart.")
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
            elif args.origin == "fhir":
                conn.execute(
                    "DELETE FROM import_meta WHERE origin = 'fhir' AND source_file IN "
                    "(SELECT fhir_base FROM sync_state WHERE source = ?)",
                    (args.source,),
                )
            if args.origin == "fhir":
                # Without its records, the next sync of that server must be a full one.
                conn.execute(
                    "DELETE FROM sync_state" + (" WHERE source = ?" if args.source else ""),
                    (args.source,) if args.source else (),
                )
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

    # Per-import breakdown, so overlapping imports (Apple Health vs direct sync) can be compared.
    conn = connect_ro()
    try:
        rows = conn.execute(
            "SELECT origin, source, resource_type, COUNT(*) n, MIN(substr(date,1,10)) first, "
            "MAX(substr(date,1,10)) last FROM resources "
            "GROUP BY origin, source, resource_type ORDER BY origin, source, n DESC"
        ).fetchall()
    finally:
        conn.close()
    current = None
    for row in rows:
        if (row["origin"], row["source"]) != current:
            current = (row["origin"], row["source"])
            print(f"\n{row['origin']} — {row['source'] or 'unknown source'}")
        span = f"{row['first'] or '?'} – {row['last'] or '?'}"
        print(f"  {row['resource_type']:<20} {row['n']:>6}  {span}")
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


def _schedule_config(args: argparse.Namespace) -> int:
    import plistlib

    if not args.fhir_base:
        print("Pass --fhir-base or set MYCHART_MCP_FHIR_BASE.", file=sys.stderr)
        return 1
    log = Path.home() / "Library" / "Logs" / "mychart-mcp-sync.log"
    job: dict = {
        "Label": args.label,
        "ProgramArguments": [
            _server_entry()["command"],
            "sync",
            "--unattended",
            "--fhir-base",
            args.fhir_base,
        ],
        "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
    }
    if "MYCHART_MCP_DB" in os.environ:
        job["EnvironmentVariables"] = {"MYCHART_MCP_DB": str(db_path())}
    sys.stdout.write(plistlib.dumps(job).decode())
    target = f"~/Library/LaunchAgents/{args.label}.plist"
    print(
        f"\n# Save the XML above to {target}, then load it:\n"
        f"#   launchctl bootstrap gui/$(id -u) {target}\n"
        f"# Run it now to test:  launchctl kickstart gui/$(id -u)/{args.label}\n"
        f"# Log: {log}",
        file=sys.stderr,
    )
    return 0


# A Claude Code folder for asking about your records. Its sessions get the mychart tools but
# no web or shell access, so text in a record can't get the model to send data anywhere.
CHAT_SETTINGS = {
    "enabledMcpjsonServers": ["mychart"],
    "permissions": {"deny": ["WebFetch", "WebSearch", "Bash"]},
}


def _claude_code_folder(folder: Path) -> int:
    files = {
        folder / ".mcp.json": {"mcpServers": {"mychart": _server_entry()}},
        folder / ".claude" / "settings.json": CHAT_SETTINGS,
    }
    existing = [str(f) for f in files if f.exists()]
    if existing:
        print("Not overwriting: " + ", ".join(existing), file=sys.stderr)
        return 1
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2) + "\n")
    print(
        f"Set up {folder} for asking about your records: run `claude` there.\n"
        "Web and shell tools are denied in that folder. If you've added other connectors\n"
        "that can send data out (email, Slack, …), deny them too in "
        f"{folder / '.claude' / 'settings.local.json'}."
    )
    return 0


def _client_config(args: argparse.Namespace) -> int:
    entry = _server_entry()
    if args.client == "claude-code":
        if not args.dir:
            print("Pass --dir, a folder to create for asking about your records.", file=sys.stderr)
            return 1
        return _claude_code_folder(Path(args.dir).expanduser())
    if args.client == "claude":
        print("# Merge into ~/Library/Application Support/Claude/claude_desktop_config.json")
        print(json.dumps({"mcpServers": {"mychart": entry}}, indent=2))
    else:
        # JSON scalars and flow sequences are valid YAML, which keeps quoting safe.
        print("# Add under mcp_servers: in a dedicated profile's config, e.g.")
        print("# ~/.hermes/profiles/health/config.yaml (hermes profile create health)")
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
    p.add_argument(
        "--full",
        action="store_true",
        help="Download everything, not just changes since the last sync (automatic every 30 days)",
    )
    p.add_argument(
        "--remember",
        action="store_true",
        help="After signing in, save a device key in the macOS Keychain for unattended syncs",
    )
    p.add_argument(
        "--unattended",
        action="store_true",
        help="Use only the saved sign-in; never open a browser (for scheduled runs)",
    )
    p.set_defaults(func=_sync)

    p = sub.add_parser("logout", help="Delete the sign-in saved by `sync --remember`")
    p.add_argument("--fhir-base", default=os.environ.get("MYCHART_MCP_FHIR_BASE"))
    p.add_argument("--sandbox", action="store_true")
    p.set_defaults(func=_logout)

    p = sub.add_parser("forget", help="Delete every record from one import source")
    p.add_argument("origin", choices=["apple_health", "fhir"])
    p.add_argument("--source", help="Only this health system (as listed by stats)")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    p.set_defaults(func=_forget)

    sub.add_parser("serve", help="Run the MCP server on stdio").set_defaults(func=_serve)
    sub.add_parser("stats", help="Show what's in the database").set_defaults(func=_stats)

    p = sub.add_parser("schedule-config", help="Print a macOS LaunchAgent that syncs daily")
    p.add_argument("--fhir-base", default=os.environ.get("MYCHART_MCP_FHIR_BASE"))
    p.add_argument("--hour", type=int, default=5)
    p.add_argument("--minute", type=int, default=0)
    p.add_argument("--label", default="io.github.mychart-mcp.sync")
    p.set_defaults(func=_schedule_config)

    p = sub.add_parser(
        "client-config", help="Print config for Claude Desktop or Hermes, or set up Claude Code"
    )
    p.add_argument("client", choices=["claude", "claude-code", "hermes"])
    p.add_argument("--dir", help="claude-code: folder to create (e.g. ~/HealthChat)")
    p.set_defaults(func=_client_config)

    args = parser.parse_args()
    sys.exit(args.func(args))
