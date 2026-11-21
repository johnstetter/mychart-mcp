"""Field extraction from synthetic FHIR resources. No real patient data."""

from __future__ import annotations

import pytest

from mychart_mcp.fhir import extract


def obs(**fields):
    return {"resourceType": "Observation", "id": "x", "code": {"text": "Sodium"}, **fields}


def q(value, unit="mmol/L"):
    return {"value": value, "unit": unit}


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([{"text": "135 - 145 mmol/L"}], (None, None, "135 - 145 mmol/L")),
        ([{"low": q(135), "high": q(145)}], (135.0, 145.0, "135 - 145 mmol/L")),
        (
            [{"low": q(135), "high": q(145), "text": "normal: 135-145"}],
            (135.0, 145.0, "normal: 135-145"),
        ),
        ([{"low": q(3.5)}], (3.5, None, ">= 3.5 mmol/L")),
        ([{"high": q(200, "mg/dL")}], (None, 200.0, "<= 200 mg/dL")),
        ([{"high": {"value": 5}}], (None, 5.0, "<= 5")),
        (
            [{"low": q(135), "high": q(145)}, {"low": q(130), "high": q(150)}],
            (135.0, 145.0, "135 - 145 mmol/L (+1 more, see get_record)"),
        ),
        ([], (None, None, None)),
        (None, (None, None, None)),
    ],
    ids=[
        "text",
        "low-high",
        "text-wins",
        "low-only",
        "high-only",
        "no-unit",
        "multiple",
        "empty",
        "none",
    ],
)
def test_reference_range(ranges, expected):
    resource = obs(valueQuantity=q(140))
    if ranges is not None:
        resource["referenceRange"] = ranges
    x = extract(resource)
    assert (x.ref_low, x.ref_high, x.ref_range) == expected


@pytest.mark.parametrize(
    ("interp", "expected"),
    [
        ([{"coding": [{"code": "H", "display": "High"}]}], "H"),
        ([{"coding": [{"code": "L"}]}], "L"),
        ([{"text": "Abnormal"}], "Abnormal"),
        ([{"coding": [{"code": "H"}]}, {"coding": [{"code": "A"}]}], "H, A"),
        ({"coding": [{"code": "HH"}]}, "HH"),  # DSTU2: a single CodeableConcept
        ([], None),
        (None, None),
    ],
    ids=["high", "low", "text-only", "several", "dstu2", "empty", "none"],
)
def test_interpretation(interp, expected):
    resource = obs(valueQuantity=q(150))
    if interp is not None:
        resource["interpretation"] = interp
    assert extract(resource).interpretation == expected


def test_non_observation_has_no_range():
    x = extract({"resourceType": "Condition", "id": "c", "code": {"text": "Asthma"}})
    assert (x.ref_low, x.ref_high, x.ref_range, x.interpretation) == (None, None, None, None)
