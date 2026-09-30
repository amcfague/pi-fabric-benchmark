"""Deterministic grading for the read-only triage case."""

import json
from pathlib import Path

_EXPECTED = json.loads(Path(__file__).with_name("triage.json").read_text())


def verify(answer: object) -> dict[str, object]:
    """Require all three findings and the combined conclusion."""
    if isinstance(answer, dict):
        findings = answer.get("findings")
        catalog = findings.get("catalog") if isinstance(findings, dict) else None
        expression = catalog.get("correct_expression") if isinstance(catalog, dict) else None
        if isinstance(expression, str) and "".join(expression.split()) == "max(stock-reserved,0)":
            answer = {
                **answer,
                "findings": {
                    **findings,
                    "catalog": {**catalog, "correct_expression": "max(0, stock - reserved)"},
                },
            }
    if answer == _EXPECTED:
        return {"passed": True, "errors": []}
    if not isinstance(answer, dict):
        return {"passed": False, "errors": ["answer must be a JSON object"]}

    errors = [key for key in ("findings", "conclusion") if answer.get(key) != _EXPECTED[key]]
    if set(answer) != set(_EXPECTED):
        errors.append("unexpected or missing top-level keys")
    return {"passed": False, "errors": errors}
