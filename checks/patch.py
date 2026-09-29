"""Independent patch grader. The writable trial receives only the fixture."""

import argparse
import importlib
import json
import sys
from pathlib import Path


def verify(project_dir: str | Path) -> dict[str, object]:
    """Run direct boundary and cross-module checks against a patched copy."""
    root = Path(project_dir).resolve()
    names = ("catalog", "billing", "shipping")
    checks: list[dict[str, object]] = []

    def check(name: str, actual: object, expected: object) -> None:
        checks.append(
            {"name": name, "passed": actual == expected, "expected": expected, "actual": actual}
        )

    if not root.is_dir():
        return {
            "passed": False, "checks": [], "errors": [f"fixture directory does not exist: {root}"]
        }

    previous = {name: sys.modules.pop(name, None) for name in names}
    root_string = str(root)
    sys.path.insert(0, root_string)
    errors: list[str] = []
    try:
        catalog, billing, shipping = (importlib.import_module(name) for name in names)
        check("inventory subtracts reservations", catalog.available_units(11, 4), 7)
        check("over-reservation clamps to zero", catalog.available_units(2, 5), 0)
        check("subtotal multiplies cents", billing.subtotal_cents(1250, 4), 5000)
        check("zero quantity is free", billing.subtotal_cents(1250, 0), 0)
        check("free shipping includes threshold", shipping.shipping_fee_cents(5000), 0)
        check("below threshold pays standard fee", shipping.shipping_fee_cents(4999), 599)
        check(
            "checkout integration",
            shipping.quote_order(11, 4, 1250, 4),
            {
                "available_units": 7,
                "order_fulfillable": True,
                "subtotal_cents": 5000,
                "shipping_fee_cents": 0,
            },
        )
    except Exception as exc:
        errors.append(f"fixture check failed to execute: {type(exc).__name__}: {exc}")
    finally:
        for name in names:
            sys.modules.pop(name, None)
        for name, module in previous.items():
            if module is not None:
                sys.modules[name] = module
        if root_string in sys.path:
            sys.path.remove(root_string)

    errors.extend(check["name"] for check in checks if not check["passed"])
    return {"passed": not errors, "checks": checks, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    args = parser.parse_args()
    result = verify(args.project_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
