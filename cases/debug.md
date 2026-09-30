# Failure-driven repair

The benchmark captured failing contract tests before the timed work. Use their failures to locate the root cause in `catalog.py`, `billing.py`, and `shipping.py`. Fix the production code without weakening or editing tests. The benchmark runs the final checks independently. Preserve public APIs and integer-cent arithmetic.