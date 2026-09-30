"""Deterministic grading for the one-file read-only control."""

_EXPECTED = {"observed_cents": 1254, "required_cents": 5000}


def verify(answer: object) -> dict[str, object]:
    if not isinstance(answer, dict):
        return {"passed": False, "errors": ["answer must be a JSON object"]}
    checks = [
        {"name": key, "passed": answer.get(key) == value,
         "expected": value, "actual": answer.get(key)}
        for key, value in _EXPECTED.items()
    ]
    errors = [check["name"] for check in checks if not check["passed"]]
    if set(answer) != set(_EXPECTED):
        errors.append("unexpected or missing keys")
    return {"passed": not errors, "checks": checks, "errors": errors}
