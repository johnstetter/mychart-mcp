"""Epic FHIR sync against a fake in-memory FHIR server. Synthetic data; no network."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from mychart_mcp import epic, server
from mychart_mcp.db import connect_rw

BASE = "https://fhir.example.test/api/FHIR/R4"
NOTE = "<p>Seen for seasonal allergies. Advised saline rinse.</p>"


def bundle(*resources, next_url=None):
    b = {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


def lab(rid, date, value):
    return {
        "resourceType": "Observation",
        "id": rid,
        "code": {"text": "Hemoglobin A1c"},
        "effectiveDateTime": date,
        "valueQuantity": {"value": value, "unit": "%"},
    }


ROUTES = {
    "Patient/p1": {
        "resourceType": "Patient",
        "id": "p1",
        "managingOrganization": {"display": "Example Health"},
    },
    "Condition?patient=p1&category=problem-list-item": bundle(
        {
            "resourceType": "Condition",
            "id": "c1",
            "code": {"text": "Seasonal allergic rhinitis"},
            "onsetDateTime": "2018-04-01",
        },
        # Epic appends warnings as OperationOutcome entries.
        {"resourceType": "OperationOutcome", "id": "warn"},
    ),
    "Observation?patient=p1&category=laboratory": bundle(
        lab("o1", "2020-05-01", 5.6), next_url=f"{BASE}/Observation?page=2"
    ),
    "Observation?page=2": bundle(lab("o2", "2022-05-01", 5.9)),
    "DocumentReference?patient=p1&category=clinical-note": bundle(
        {
            "resourceType": "DocumentReference",
            "id": "n1",
            "type": {"text": "Progress Note"},
            "date": "2021-04-10",
            "content": [
                {"attachment": {"contentType": "text/rtf", "url": "Binary/n1-rtf"}},
                {"attachment": {"contentType": "text/html", "url": "Binary/n1-html"}},
            ],
        }
    ),
    "Binary/n1-html": NOTE.encode(),
}
FORBIDDEN = {"Procedure?patient=p1"}


class FakeHttp(epic.Http):
    def __init__(self, routes=ROUTES):
        super().__init__(BASE, token="t")
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url, accept="application/fhir+json"):
        rel = self.resolve(url).removeprefix(self.base)
        self.requested.append(rel)
        if rel in FORBIDDEN:
            raise epic.FhirError("HTTP 403")
        body = self.routes.get(rel, bundle())
        return body if isinstance(body, bytes) else epic.json.dumps(body).encode()


NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def run_sync(path, http, *, full=False, now=NOW):
    """What `mychart-mcp sync` does after signing in."""
    conn = connect_rw(path)
    try:
        patient = epic.fetch_patient(http, "p1")
        source = epic.source_name(patient, BASE)
        since = epic.sync_since(conn, BASE, "p1", source, full=full, now=now)
        chart = epic.fetch_chart(
            http,
            patient,
            since=since,
            stored_note=lambda note_id: epic.stored_note(conn, source, note_id),
        )
        counts = epic.store(conn, source, BASE, chart, now)
    finally:
        conn.close()
    return chart, counts


def rows(path, sql, *params):
    conn = connect_rw(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def synced(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    monkeypatch.setenv("MYCHART_MCP_DB", str(path))
    http = FakeHttp()
    chart, counts = run_sync(path, http)
    return path, http, chart.resources, chart.problems, counts


def test_fetch_follows_pages_and_skips_operation_outcomes(synced):
    _, http, resources, _, counts = synced
    assert "Observation?page=2" in http.requested
    assert counts["Observation"] == 2
    assert counts["Condition"] == 1
    assert "OperationOutcome" not in {r["resourceType"] for r in resources}


def test_forbidden_search_is_reported_not_fatal(synced):
    _, _, _, problems, counts = synced
    assert any(p.startswith("Procedure:") for p in problems)
    assert counts["Patient"] == 1


def test_note_text_fetched_as_html_and_searchable(synced):
    _, http, _, _, _ = synced
    assert "Binary/n1-html" in http.requested
    assert "Binary/n1-rtf" not in http.requested
    assert {r["resource_id"] for r in server.search_records("saline rinse")} == {"n1"}
    assert "saline rinse" in server.get_record("DocumentReference", "n1")["note_text"]


def test_source_from_managing_organization(synced):
    assert server.data_summary()["sources"] == ["Example Health"]


def test_resync_replaces_rather_than_duplicates(synced):
    path, _, resources, _, _ = synced
    run_sync(path, FakeHttp(), full=True)
    (n,) = rows(path, "SELECT COUNT(*) FROM resources")[0]
    (fts,) = rows(path, "SELECT COUNT(*) FROM resources_fts")[0]
    assert n == fts == len(resources)


# --- Incremental sync ------------------------------------------------------------------------

LATER = NOW + timedelta(days=3)


class ChangedSince(FakeHttp):
    """A server where, since the last sync, one lab was corrected and one added."""

    def get(self, url, accept="application/fhir+json"):
        rel = self.resolve(url).removeprefix(self.base)
        if "_lastUpdated=ge" in rel and rel.startswith("Observation") and "laboratory" in rel:
            self.requested.append(rel)
            corrected = lab("o2", "2022-05-01", 5.8)
            corrected["code"] = {"text": "Glycated hemoglobin"}
            return epic.json.dumps(bundle(corrected, lab("o3", "2026-10-30", 5.7))).encode()
        return super().get(url, accept)


def test_first_sync_is_full_and_records_where_to_start(synced):
    path, http, _, _, _ = synced
    assert not any("_lastUpdated" in u for u in http.requested)
    ((since, full_at),) = rows(path, "SELECT since, full_at FROM sync_state")
    assert since == "2026-10-31T12:00:00Z"  # a day of overlap
    assert full_at == NOW.isoformat()


def test_next_sync_asks_only_for_changes(synced):
    path = synced[0]
    http = FakeHttp()
    chart, _ = run_sync(path, http, now=LATER)
    assert chart.since == "2026-10-31T12:00:00Z"
    searched = [u for u in http.requested if "?patient=" in u]
    for url in searched:
        rtype = url.split("?")[0]
        assert ("_lastUpdated=ge2026-10-31T12%3A00%3A00Z" in url) == (
            rtype in epic.INCREMENTAL_TYPES
        ), url
    assert not any(u.startswith("Binary/") for u in http.requested)  # no changed notes
    # Unchanged records stay.
    ids = {r[0] for r in rows(path, "SELECT resource_id FROM resources")}
    assert {"p1", "c1", "o1", "o2", "n1"} <= ids
    assert server.search_records("saline rinse")


def test_unchanged_note_reuses_stored_text(synced):
    # Like Epic, the fake returns notes on change-only searches even when nothing changed.
    path = synced[0]
    routes = dict(ROUTES)
    query = "DocumentReference?patient=p1&category=clinical-note"
    since = "&_lastUpdated=ge2026-10-31T12%3A00%3A00Z"
    routes[query + since] = routes[query]
    http = FakeHttp(routes)
    run_sync(path, http, now=LATER)
    assert query + since in http.requested
    assert not any(u.startswith("Binary/") for u in http.requested)
    assert "saline rinse" in server.get_record("DocumentReference", "n1")["note_text"]


def test_changed_note_refetches_text(synced):
    path = synced[0]
    routes = dict(ROUTES)
    query = "DocumentReference?patient=p1&category=clinical-note"
    changed = epic.json.loads(epic.json.dumps(routes[query]))
    changed["entry"][0]["resource"]["docStatus"] = "amended"
    routes[query + "&_lastUpdated=ge2026-10-31T12%3A00%3A00Z"] = changed
    http = FakeHttp(routes)
    run_sync(path, http, now=LATER)
    assert "Binary/n1-html" in http.requested


def test_full_sync_always_refetches_note_text(synced):
    path = synced[0]
    http = FakeHttp()
    run_sync(path, http, full=True, now=LATER)
    assert "Binary/n1-html" in http.requested


def test_changed_record_replaces_its_old_copy(synced):
    path = synced[0]
    run_sync(path, ChangedSince(), now=LATER)
    labs = rows(
        path,
        "SELECT resource_id, value FROM resources WHERE resource_type = 'Observation' "
        "ORDER BY resource_id",
    )
    assert labs == [("o1", 5.6), ("o2", 5.8), ("o3", 5.7)]
    assert {r["resource_id"] for r in server.search_records("glycated")} == {"o2"}
    (n,) = rows(path, "SELECT COUNT(*) FROM resources")[0]
    (fts,) = rows(path, "SELECT COUNT(*) FROM resources_fts")[0]
    assert n == fts


def test_types_without_last_updated_are_replaced_in_full(synced):
    path = synced[0]
    routes = dict(ROUTES)
    routes["Condition?patient=p1&category=problem-list-item"] = bundle(
        {"resourceType": "Condition", "id": "c2", "code": {"text": "Contact dermatitis"}}
    )
    run_sync(path, FakeHttp(routes), now=LATER)
    conditions = rows(path, "SELECT resource_id FROM resources WHERE resource_type = 'Condition'")
    assert conditions == [("c2",)]


class SlowLabs(FakeHttp):
    def get(self, url, accept="application/fhir+json"):
        if "laboratory" in url:
            raise epic.FhirError("timed out")
        return super().get(url, accept)


def test_failed_search_keeps_that_types_rows(synced):
    path = synced[0]
    chart, _ = run_sync(path, SlowLabs(), full=True, now=LATER)
    assert "Observation" not in chart.complete
    labs = rows(path, "SELECT resource_id FROM resources WHERE resource_type = 'Observation'")
    assert sorted(labs) == [("o1",), ("o2",)]


def test_problems_keep_the_starting_point_for_incremental_syncs(synced):
    path = synced[0]
    run_sync(path, SlowLabs(), now=LATER)
    assert rows(path, "SELECT since FROM sync_state") == [("2026-10-31T12:00:00Z",)]
    run_sync(path, FakeHttp(), now=LATER)
    assert rows(path, "SELECT since FROM sync_state") == [("2026-11-03T12:00:00Z",)]


def test_missing_note_text_keeps_the_starting_point(synced):
    path = synced[0]
    routes = dict(ROUTES)
    query = "DocumentReference?patient=p1&category=clinical-note"
    changed = epic.json.loads(epic.json.dumps(routes[query]))
    changed["entry"][0]["resource"]["docStatus"] = "amended"
    routes[query + "&_lastUpdated=ge2026-10-31T12%3A00%3A00Z"] = changed
    del routes["Binary/n1-html"]

    class NoNotes(FakeHttp):
        def get(self, url, accept="application/fhir+json"):
            if url.startswith("Binary/"):
                raise epic.FhirError("HTTP 404")
            return super().get(url, accept)

    chart, _ = run_sync(path, NoNotes(routes), now=LATER)
    assert chart.problems["note text unavailable"] == 1
    assert rows(path, "SELECT since FROM sync_state") == [("2026-10-31T12:00:00Z",)]


def test_when_a_full_sync_is_needed(synced, monkeypatch):
    path = synced[0]
    conn = connect_rw(path)
    try:

        def since(**kw):
            args = {"fhir_base": BASE, "patient_id": "p1", "source": "Example Health", **kw}
            args.setdefault("now", LATER)
            return epic.sync_since(conn, **args)

        assert since() == "2026-10-31T12:00:00Z"
        assert since(full=True) is None
        assert since(patient_id="p2") is None
        assert since(source="Other Health") is None
        assert since(fhir_base="https://other.example.test/R4") is None
        assert since(now=NOW + epic.FULL_SYNC_EVERY + timedelta(minutes=1)) is None
        monkeypatch.setattr(epic, "SEARCHES", [*epic.SEARCHES, ("Goal", {})])
        assert since() is None
    finally:
        conn.close()


def test_incremental_sync_keeps_full_sync_date(synced):
    path = synced[0]
    run_sync(path, FakeHttp(), now=LATER)
    assert rows(path, "SELECT full_at FROM sync_state") == [(NOW.isoformat(),)]


def test_token_never_sent_off_host():
    with pytest.raises(epic.FhirError, match="Refusing"):
        epic.Http(BASE, token="t").resolve("https://elsewhere.test/Observation?page=2")


def test_pkce_and_authorize_url():
    verifier, challenge = epic.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()

    url = epic.authorize_url(
        {"authorization_endpoint": "https://auth.example.test/authorize"},
        client_id="cid",
        redirect_uri=epic.DEFAULT_REDIRECT,
        fhir_base=BASE,
        state="s",
        challenge=challenge,
    )
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert params["aud"] == BASE
    assert params["code_challenge_method"] == "S256"
    assert "write" not in params["scope"]
    assert "offline_access" not in params["scope"]

    offline = epic.authorize_url(
        {"authorization_endpoint": "https://auth.example.test/authorize"},
        client_id="cid",
        redirect_uri=epic.DEFAULT_REDIRECT,
        fhir_base=BASE,
        state="s",
        challenge=challenge,
        offline=True,
    )
    assert (
        "offline_access"
        in dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(offline).query))["scope"]
    )


def test_callback_ignores_wrong_state():
    listener = epic.CallbackListener("http://127.0.0.1:0/callback")
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=listener.wait("good", 10)))
    thread.start()
    for state in ("bad", "good"):
        urllib.request.urlopen(
            f"http://127.0.0.1:{listener.port}/callback?code=c-{state}&state={state}", timeout=5
        ).read()
    thread.join(10)
    assert result["code"] == "c-good"


def test_listener_port_can_be_reused_right_away():
    first = epic.CallbackListener("http://127.0.0.1:0/callback")
    port = first.port
    thread = threading.Thread(target=lambda: first.wait("s", 10))
    thread.start()
    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=c&state=s", timeout=5).read()
    thread.join(10)
    epic.CallbackListener(f"http://127.0.0.1:{port}/callback").server.server_close()


def test_redirect_must_be_loopback():
    with pytest.raises(epic.FhirError, match="loopback"):
        epic.CallbackListener("http://example.test:8765/callback")


def run_cli(monkeypatch, *argv):
    from mychart_mcp import cli

    monkeypatch.setattr(cli.sys, "argv", ["mychart-mcp", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_forget_removes_only_that_origin(synced, monkeypatch):
    path, _, resources, _, _ = synced
    conn = connect_rw(path)
    epic.insert_resource(conn, "apple_health", "Example Health", resources[1])
    conn.commit()
    conn.close()

    assert run_cli(monkeypatch, "forget", "fhir", "--yes") == 0
    conn = connect_rw(path)
    origins = conn.execute("SELECT DISTINCT origin FROM resources").fetchall()
    fts = conn.execute("SELECT COUNT(*) FROM resources_fts").fetchone()[0]
    meta = conn.execute("SELECT origin FROM import_meta").fetchall()
    conn.close()
    assert origins == [("apple_health",)]
    assert fts == 1
    assert meta == []
    assert rows(path, "SELECT * FROM sync_state") == []


def test_forget_asks_first(synced, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run_cli(monkeypatch, "forget", "fhir") == 1
    assert server.data_summary()["record_types"]


class Scripted:
    """A loopback HTTP/1.1 server that answers each request with the next scripted reply.

    A reply is a status code, "slow" (sleep past the client's timeout), or "drop" (answer,
    then close the connection without saying so, like a server timing out an idle one).
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.ports: list[int] = []
        self.paths: list[str] = []
        scripted = self

        class Handler(epic.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                scripted.ports.append(self.client_address[1])
                scripted.paths.append(self.path)
                reply = scripted.replies.pop(0) if scripted.replies else 200
                if reply == "slow":
                    threading.Event().wait(1)  # time.sleep is patched out below
                    reply = 200
                if isinstance(reply, tuple):  # (status, Location)
                    self.send_response(reply[0])
                    self.send_header("Location", reply[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"resourceType": "Bundle"}' if reply in (200, "drop") else b""
                self.send_response(200 if reply == "drop" else reply)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if reply == "drop":
                    self.close_connection = True

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/R4/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def scripted(monkeypatch):
    monkeypatch.setattr(epic.time, "sleep", lambda _: None)  # no backoff between retries
    servers = []

    def make(*replies):
        servers.append(Scripted(*replies))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_one_connection_for_many_requests(scripted):
    srv = scripted()
    http = epic.Http(srv.base)
    for _ in range(5):
        assert http.get_json("Observation") == {"resourceType": "Bundle"}
    assert len(srv.ports) == 5
    assert len(set(srv.ports)) == 1


def test_reconnects_when_the_server_drops_an_idle_connection(scripted):
    srv = scripted("drop", 200)
    http = epic.Http(srv.base)
    http.get("Observation")
    http.get("Observation")
    assert len(srv.paths) == 2
    assert len(set(srv.ports)) == 2


def test_timeouts_and_gateway_errors_are_retried(scripted):
    srv = scripted("slow", 504, 200)
    assert epic.Http(srv.base, timeout=0.3).get_json("Observation") == {"resourceType": "Bundle"}
    assert len(srv.paths) == 3


def test_client_errors_are_not_retried(scripted):
    srv = scripted(403)
    with pytest.raises(epic.FhirError, match="HTTP 403"):
        epic.Http(srv.base).get("Procedure")
    assert len(srv.paths) == 1


def test_redirects_stay_on_the_same_host(scripted):
    srv = scripted((302, "/R4/Binary/moved"), 200, (302, "https://elsewhere.test/Binary/x"))
    http = epic.Http(srv.base)
    assert http.get("Binary/n1") == b'{"resourceType": "Bundle"}'
    assert srv.paths[:2] == ["/R4/Binary/n1", "/R4/Binary/moved"]
    with pytest.raises(epic.FhirError, match="Refusing"):
        http.get("Binary/n2")


def test_persistent_timeout_skips_that_search_only(monkeypatch):
    lines = []
    http = SlowLabs()
    chart = epic.fetch_chart(http, epic.fetch_patient(http, "p1"), progress=lines.append)
    assert any(p.startswith("Observation (laboratory):") for p in chart.problems)
    assert any("DocumentReference" in r["resourceType"] for r in chart.resources)
    assert any("skipped" in line for line in lines)


def test_stats_breaks_down_by_origin(synced, monkeypatch, capsys):
    assert run_cli(monkeypatch, "stats") == 0
    out = capsys.readouterr().out
    assert "fhir — Example Health" in out


def test_claude_code_folder_denies_outbound_tools(tmp_path, monkeypatch):
    folder = tmp_path / "HealthChat"
    assert run_cli(monkeypatch, "client-config", "claude-code", "--dir", str(folder)) == 0
    settings = json.loads((folder / ".claude" / "settings.json").read_text())
    assert {"WebFetch", "WebSearch", "Bash"} <= set(settings["permissions"]["deny"])
    assert settings["enabledMcpjsonServers"] == ["mychart"]
    mcp = json.loads((folder / ".mcp.json").read_text())
    assert mcp["mcpServers"]["mychart"]["args"] == ["serve"]
    # Never overwrite an existing setup.
    assert run_cli(monkeypatch, "client-config", "claude-code", "--dir", str(folder)) == 1


def test_dev_repo_denies_the_records_server():
    from pathlib import Path

    settings = json.loads(Path(".claude/settings.json").read_text())
    assert "mcp__mychart" in settings["permissions"]["deny"]
    assert not Path(".mcp.json").exists()
