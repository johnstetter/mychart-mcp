"""End-to-end tests against a synthetic Apple Health export. No real patient data."""

from __future__ import annotations

import base64
import json
import sqlite3
import zipfile

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mychart_mcp import server
from mychart_mcp.db import connect_ro, connect_rw
from mychart_mcp.importers import import_apple_health

ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"

NOTE = (
    "<p>Patient reports wheezing with exercise. Spirometry shows FEV1 78%. "
    "Dx: mild persistent asthma.</p>"
)

RESOURCES = {
    "Condition-asthma.json": {
        "resourceType": "Condition",
        "id": "asthma",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {
            "text": "Mild persistent asthma",
            "coding": [
                {"system": ICD10, "code": "J45.30"},
                {"system": SNOMED, "code": "426979002"},
            ],
        },
        "recordedDate": "2021-03-02",
    },
    # Older DSTU2-style resource with no onset, only dateRecorded.
    "Condition-rhinitis.json": {
        "resourceType": "Condition",
        "id": "rhinitis",
        "code": {
            "coding": [{"system": ICD10, "code": "J30.2", "display": "Seasonal allergic rhinitis"}]
        },
        "dateRecorded": "2015-06-01",
    },
    "Encounter-visit.json": {
        "resourceType": "Encounter",
        "id": "visit",
        "type": [{"text": "Office Visit"}],
        "reasonCode": [
            {
                "text": "Wheezing, suspected asthma",
                "coding": [{"system": ICD10, "code": "R06.2"}],
            }
        ],
        "period": {"start": "2019-11-04T09:00:00Z", "end": "2019-11-04T09:30:00Z"},
    },
    "Procedure-pft.json": {
        "resourceType": "Procedure",
        "id": "pft",
        "code": {"text": "Spirometry, asthma evaluation"},
        "performedDateTime": "2020-01-15",
    },
    "Observation-a1c-1.json": {
        "resourceType": "Observation",
        "id": "a1c-1",
        "code": {"text": "Hemoglobin A1c", "coding": [{"system": LOINC, "code": "4548-4"}]},
        "effectiveDateTime": "2022-05-01",
        "valueQuantity": {"value": 5.9, "unit": "%"},
    },
    "Observation-a1c-2.json": {
        "resourceType": "Observation",
        "id": "a1c-2",
        "code": {"text": "Hemoglobin A1c", "coding": [{"system": LOINC, "code": "4548-4"}]},
        "effectiveDateTime": "2020-05-01",
        "valueQuantity": {"value": 5.6, "unit": "%"},
    },
    "DocumentReference-note.json": {
        "resourceType": "DocumentReference",
        "id": "note",
        "type": {"text": "Progress Note"},
        "date": "2020-02-01T10:00:00Z",
        "content": [
            {
                "attachment": {
                    "contentType": "text/html",
                    "data": base64.b64encode(NOTE.encode()).decode(),
                }
            }
        ],
    },
    # Bundles should be unpacked.
    "Bundle-meds.json": {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": "inhaler",
                    "medicationCodeableConcept": {"text": "Albuterol inhaler"},
                    "authoredOn": "2020-03-01",
                }
            }
        ],
    },
}

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [<!ELEMENT HealthData ANY>]>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierStepCount" value="1000"/>
 {records}
</HealthData>
"""


@pytest.fixture()
def export_zip(tmp_path):
    path = tmp_path / "export.zip"
    records = "\n".join(
        f'<ClinicalRecord type="x" identifier="{name}" sourceName="Test Health" '
        f'resourceFilePath="/clinical-records/{name}"/>'
        for name in RESOURCES
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", EXPORT_XML.format(records=records))
        for name, resource in RESOURCES.items():
            zf.writestr(f"apple_health_export/clinical-records/{name}", json.dumps(resource))
    return path


@pytest.fixture()
def db(tmp_path, monkeypatch, export_zip):
    path = tmp_path / "health.db"
    monkeypatch.setenv("MYCHART_MCP_DB", str(path))
    conn = connect_rw(path)
    counts = import_apple_health(conn, export_zip)
    conn.close()
    return path, counts


def test_import_counts_and_permissions(db):
    path, counts = db
    assert counts["Condition"] == 2
    assert counts["Observation"] == 2
    assert counts["MedicationRequest"] == 1
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_reimport_replaces_rather_than_duplicates(db, export_zip):
    path, _ = db
    conn = connect_rw(path)
    import_apple_health(conn, export_zip)
    n = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM resources_fts").fetchone()[0]
    conn.close()
    assert n == fts == 8


def test_source_attribution(db):
    assert server.data_summary()["sources"] == ["Test Health"]


def test_search_by_name_code_and_note_text(db):
    def ids(rs):
        return {r["resource_id"] for r in rs}

    assert "asthma" in ids(server.search_records("asthma"))
    assert ids(server.search_records("J45.30")) == {"asthma"}
    assert "note" in ids(server.search_records("wheezing spirometry"))


def test_condition_timeline_finds_earliest_evidence(db):
    result = server.condition_timeline("asthma")
    dates = [r.get("date") for r in result["records"]]
    assert dates == sorted(dates)
    assert result["records"][0]["resource_id"] == "visit"
    assert result["earliest_by_type"]["Condition"]["date"] == "2021-03-02"
    assert result["earliest_by_type"]["Procedure"]["date"] == "2020-01-15"


def test_dstu2_recorded_date_used(db):
    rhinitis = server.search_records("allergic rhinitis")[0]
    assert rhinitis["date"] == "2015-06-01"


def test_lab_trend_is_oldest_first_with_values(db):
    labs = server.lab_results("a1c")
    assert [(r["date"], r["value"]) for r in labs] == [("2020-05-01", 5.6), ("2022-05-01", 5.9)]


def test_date_filters(db):
    labs = server.lab_results("a1c", date_from="2021-01-01")
    assert [r["resource_id"] for r in labs] == ["a1c-1"]


def test_get_record_decodes_note_and_drops_base64(db):
    rec = server.get_record("DocumentReference", "note")
    assert "mild persistent asthma" in rec["note_text"]
    assert "<p>" not in rec["note_text"]
    assert "data" not in rec["resource"]["content"][0]["attachment"]


def test_server_connection_is_read_only(db):
    conn = connect_ro()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM resources")


def test_fts_syntax_in_query_is_neutralized(db):
    # Quotes, operators and parentheses must not reach FTS5 as syntax.
    assert server.search_records('asthma" OR (NEAR') == []
    with pytest.raises(ToolError):
        server.search_records("***")


def test_missing_database_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCHART_MCP_DB", str(tmp_path / "missing.db"))
    with pytest.raises(ToolError, match="mychart-mcp import"):
        server.data_summary()


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {}, "Library/Application Support/mychart-mcp/health.db"),
        ("linux", {}, ".local/share/mychart-mcp/health.db"),
        ("linux", {"XDG_DATA_HOME": "/data"}, "/data/mychart-mcp/health.db"),
    ],
)
def test_default_db_path_per_platform(monkeypatch, platform, env, expected):
    from mychart_mcp import db

    monkeypatch.setattr(db.sys, "platform", platform)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert str(db._default_db()).endswith(expected)
