"""Load records into the local database. Only Apple Health exports for now."""

from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .fhir import extract, iter_resources

APPLE_ORIGIN = "apple_health"


def _clinical_sources(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map clinical-records file name -> organization name using export.xml.

    export.xml can be gigabytes of fitness data, so stream it and keep only
    ClinicalRecord elements.
    """
    xml_name = next((n for n in zf.namelist() if PurePosixPath(n).name == "export.xml"), None)
    if xml_name is None:
        return {}
    sources: dict[str, str] = {}
    with zf.open(xml_name) as fh:
        for _, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == "ClinicalRecord":
                path = elem.get("resourceFilePath")
                if path:
                    sources[PurePosixPath(path).name] = elem.get("sourceName") or ""
            elem.clear()
    return sources


def import_apple_health(conn: sqlite3.Connection, zip_path: Path) -> Counter:
    """Replace all Apple Health rows with the contents of an export.zip."""
    counts: Counter = Counter()
    with zipfile.ZipFile(zip_path) as zf:
        members = [
            n for n in zf.namelist() if "/clinical-records/" in f"/{n}" and n.endswith(".json")
        ]
        if not members:
            raise ValueError(
                f"{zip_path} has no clinical-records/*.json. Make sure Health Records "
                "finished syncing before exporting."
            )
        sources = _clinical_sources(zf)

        with conn:
            conn.execute(
                "DELETE FROM resources_fts WHERE rowid IN "
                "(SELECT rowid FROM resources WHERE origin = ?)",
                (APPLE_ORIGIN,),
            )
            conn.execute("DELETE FROM resources WHERE origin = ?", (APPLE_ORIGIN,))

            for name in members:
                with zf.open(name) as fh:
                    try:
                        doc = json.load(fh)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        counts["_unparseable"] += 1
                        continue
                source = sources.get(PurePosixPath(name).name)
                for resource in iter_resources(doc):
                    if _insert(conn, APPLE_ORIGIN, source, resource):
                        counts[resource["resourceType"]] += 1
                    else:
                        counts["_duplicate"] += 1

            conn.execute(
                "INSERT OR REPLACE INTO import_meta VALUES (?, ?, ?, ?)",
                (
                    APPLE_ORIGIN,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    zip_path.name,
                    json.dumps(counts),
                ),
            )
    return counts


def _insert(conn: sqlite3.Connection, origin: str, source: str | None, resource: dict) -> bool:
    x = extract(resource)
    cur = conn.execute(
        """INSERT OR IGNORE INTO resources
           (origin, source, resource_type, resource_id, date, recorded, end_date, status,
            display, codes, value, unit, value_text, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            origin,
            source or "",
            x.resource_type,
            x.resource_id,
            x.date,
            x.recorded,
            x.end_date,
            x.status,
            x.display,
            " ".join(x.codes),
            x.value,
            x.unit,
            x.value_text,
            json.dumps(resource, separators=(",", ":")),
        ),
    )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO resources_fts (rowid, display, codes, body) VALUES (?, ?, ?, ?)",
        (cur.lastrowid, x.display or "", " ".join(x.codes), x.body),
    )
    return True
