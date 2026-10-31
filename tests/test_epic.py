"""Epic FHIR sync against a fake in-memory FHIR server. Synthetic data; no network."""

from __future__ import annotations

import base64
import hashlib
import threading
import urllib.parse
import urllib.request

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


@pytest.fixture()
def synced(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    monkeypatch.setenv("MYCHART_MCP_DB", str(path))
    http = FakeHttp()
    resources, problems = epic.fetch_chart(http, "p1")
    conn = connect_rw(path)
    counts = epic.store(conn, epic.source_name(resources[0], BASE), BASE, resources)
    conn.close()
    return path, http, resources, problems, counts


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
    conn = connect_rw(path)
    epic.store(conn, "Example Health", BASE, resources)
    n = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM resources_fts").fetchone()[0]
    conn.close()
    assert n == fts == len(resources)


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


def test_forget_asks_first(synced, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run_cli(monkeypatch, "forget", "fhir") == 1
    assert server.data_summary()["record_types"]
