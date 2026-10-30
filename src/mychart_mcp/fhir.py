"""Pull the searchable fields out of a FHIR resource (R4, with DSTU2 fallbacks)."""

from __future__ import annotations

import base64
import binascii
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

SYSTEM_ALIASES = {
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD10",
    "http://hl7.org/fhir/sid/icd-10": "ICD10",
    "urn:oid:2.16.840.1.113883.6.90": "ICD10",
    "http://hl7.org/fhir/sid/icd-9-cm": "ICD9",
    "urn:oid:2.16.840.1.113883.6.103": "ICD9",
    "http://snomed.info/sct": "SNOMED",
    "urn:oid:2.16.840.1.113883.6.96": "SNOMED",
    "http://loinc.org": "LOINC",
    "urn:oid:2.16.840.1.113883.6.1": "LOINC",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RXNORM",
    "urn:oid:2.16.840.1.113883.6.88": "RXNORM",
    "http://www.ama-assn.org/go/cpt": "CPT",
    "urn:oid:2.16.840.1.113883.6.12": "CPT",
    "http://hl7.org/fhir/sid/cvx": "CVX",
}

# First non-empty path wins. Paths are dotted; list elements use the first item.
DATE_PATHS = {
    "Condition": ["onsetDateTime", "onsetPeriod.start", "recordedDate", "dateRecorded"],
    "Encounter": ["period.start"],
    "Observation": ["effectiveDateTime", "effectivePeriod.start", "effectiveInstant", "issued"],
    "DiagnosticReport": ["effectiveDateTime", "effectivePeriod.start", "issued"],
    "Procedure": ["performedDateTime", "performedPeriod.start"],
    "MedicationRequest": ["authoredOn"],
    "MedicationOrder": ["dateWritten"],
    "MedicationStatement": ["effectiveDateTime", "effectivePeriod.start", "dateAsserted"],
    "Immunization": ["occurrenceDateTime", "date"],
    "AllergyIntolerance": ["onsetDateTime", "recordedDate"],
    "DocumentReference": ["context.period.start", "date", "created", "indexed"],
    "CarePlan": ["period.start", "created"],
}
GENERIC_DATE_PATHS = ["effectiveDateTime", "period.start", "date", "issued", "authoredOn"]

RECORDED_PATHS = ["recordedDate", "dateRecorded", "issued", "authoredOn", "dateWritten"]
END_PATHS = [
    "abatementDateTime",
    "abatementPeriod.end",
    "period.end",
    "effectivePeriod.end",
    "performedPeriod.end",
    "context.period.end",
]

CONCEPT_PATHS = [
    "code",
    "medicationCodeableConcept",
    "vaccineCode",
    "substance",
    "type",
    "category",
]

SKIP_TEXT_KEYS = {
    "reference",
    "url",
    "system",
    "id",
    "fullUrl",
    "data",
    "resourceType",
    "div",
    "contentType",
    "language",
}


@dataclass
class Extracted:
    resource_type: str
    resource_id: str
    date: str | None = None
    recorded: str | None = None
    end_date: str | None = None
    status: str | None = None
    display: str | None = None
    codes: list[str] = field(default_factory=list)
    value: float | None = None
    unit: str | None = None
    value_text: str | None = None
    body: str = ""


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    return obj


def _first(resource: dict, paths: list[str]) -> Any:
    for path in paths:
        value = _get(resource, path)
        if value not in (None, "", [], {}):
            return value
    return None


def _concept_display(concept: Any) -> str | None:
    if not isinstance(concept, dict):
        return None
    if concept.get("text"):
        return concept["text"]
    for coding in concept.get("coding") or []:
        if coding.get("display"):
            return coding["display"]
    return None


def _concept_codes(concept: Any) -> Iterator[str]:
    concepts = concept if isinstance(concept, list) else [concept]
    for c in concepts:
        if not isinstance(c, dict):
            continue
        for coding in c.get("coding") or []:
            code = coding.get("code")
            if code:
                alias = SYSTEM_ALIASES.get(coding.get("system", ""), "OTHER")
                yield f"{alias}:{code}"


def _status(resource: dict) -> str | None:
    status = resource.get("clinicalStatus") or resource.get("status")
    if isinstance(status, dict):
        return _concept_display(status) or next(
            (c.get("code") for c in status.get("coding") or [] if c.get("code")), None
        )
    return status


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _attachment_text(resource: dict) -> Iterator[str]:
    """Decode inline text/HTML attachments (clinical notes in DocumentReference)."""
    for content in resource.get("content") or []:
        attachment = content.get("attachment") or {}
        content_type = attachment.get("contentType", "")
        data = attachment.get("data")
        if not data or not (
            content_type.startswith("text/") or "html" in content_type or "rtf" in content_type
        ):
            continue
        try:
            text = base64.b64decode(data).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            continue
        yield _strip_html(text) if "html" in content_type else text


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key not in SKIP_TEXT_KEYS:
                yield from _strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _strings(item)
    elif isinstance(obj, str) and not obj.startswith(("http://", "https://", "urn:")):
        yield obj


def extract(resource: dict) -> Extracted:
    rtype = resource.get("resourceType", "Unknown")
    out = Extracted(resource_type=rtype, resource_id=str(resource.get("id", "")))

    out.date = _first(resource, DATE_PATHS.get(rtype, GENERIC_DATE_PATHS))
    out.recorded = _first(resource, RECORDED_PATHS)
    out.end_date = _first(resource, END_PATHS)
    out.status = _status(resource)

    concept = next((resource[p] for p in CONCEPT_PATHS if resource.get(p)), None)
    if isinstance(concept, list):
        concept = concept[0]
    out.display = _concept_display(concept) or resource.get("description")

    codes: list[str] = []
    for path in CONCEPT_PATHS + ["reasonCode", "bodySite"]:
        codes.extend(_concept_codes(resource.get(path)))
    out.codes = list(dict.fromkeys(codes))

    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict) and isinstance(quantity.get("value"), (int, float)):
        out.value = float(quantity["value"])
        out.unit = quantity.get("unit") or quantity.get("code")
    else:
        out.value_text = resource.get("valueString") or _concept_display(
            resource.get("valueCodeableConcept")
        )

    parts = list(_strings(resource))
    parts.extend(_attachment_text(resource))
    narrative = _get(resource, "text.div")
    if isinstance(narrative, str):
        parts.append(_strip_html(narrative))
    out.body = "\n".join(p for p in parts if p.strip())
    return out


def iter_resources(doc: Any) -> Iterator[dict]:
    """Yield resources from a single resource or a Bundle."""
    if not isinstance(doc, dict):
        return
    if doc.get("resourceType") == "Bundle":
        for entry in doc.get("entry") or []:
            yield from iter_resources(entry.get("resource"))
    elif doc.get("resourceType"):
        yield doc
