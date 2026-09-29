"""Deterministic grading for the read-only triage case."""

import json
from pathlib import Path

_EXPECTED = json.loads(Path(__file__).with_name("triage.json").read_text())


def verify(answer: object) -> dict[str, object]:
    """Require all three findings and the combined conclusion."""
    if answer == _EXPECTED:
        return {"passed": True, "errors": []}
    if not isinstance(answer, dict):
        return {"passed": False, "errors": ["answer must be a JSON object"]}

    errors = [key for key in ("findings", "conclusion") if answer.get(key) != _EXPECTED[key]]
    if set(answer) != set(_EXPECTED):
        errors.append("unexpected or missing top-level keys")
    return {"passed": False, "errors": errors}
