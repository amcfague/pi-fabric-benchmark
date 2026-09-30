# Failure-driven repair

Run `python3 -m unittest test_contracts` from the project root before editing. Use the three failing tests to locate the root cause in `catalog.py`, `billing.py`, and `shipping.py`. Fix the production code without weakening or editing tests, then rerun the suite. Preserve public APIs and integer-cent arithmetic.