import json
import shutil
import tempfile
import unittest
from pathlib import Path

from checks.patch import verify as verify_patch
from checks.triage import verify as verify_triage

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "project"
TRIAGE = json.loads((ROOT / "checks" / "triage.json").read_text())


class TriageChecks(unittest.TestCase):
    def test_complete_answer_passes(self):
        self.assertTrue(verify_triage(TRIAGE)["passed"])

    def test_missing_finding_or_combined_conclusion_fails(self):
        answer = json.loads(json.dumps(TRIAGE))
        del answer["findings"]["catalog"]
        self.assertFalse(verify_triage(answer)["passed"])

        answer = json.loads(json.dumps(TRIAGE))
        del answer["conclusion"]
        self.assertFalse(verify_triage(answer)["passed"])

    def test_non_object_answer_fails_cleanly(self):
        self.assertFalse(verify_triage(None)["passed"])


class PatchChecks(unittest.TestCase):
    def copy_fixture(self, parent):
        target = Path(parent) / "project"
        shutil.copytree(FIXTURE, target)
        return target

    def patch(self, project, modules):
        fixes = {
            "catalog.py": (
                "return stock + reserved", "return max(0, stock - reserved)"
            ),
            "billing.py": (
                "return unit_price_cents + quantity",
                "return unit_price_cents * quantity",
            ),
            "shipping.py": (
                "if subtotal_cents > free_threshold_cents:",
                "if subtotal_cents >= free_threshold_cents:",
            ),
        }
        for name in modules:
            path = project / name
            source = path.read_text()
            old, new = fixes[name]
            self.assertIn(old, source)
            path.write_text(source.replace(old, new))

    def test_seed_and_two_fixes_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = self.copy_fixture(tmp)
            self.assertFalse(verify_patch(seed)["passed"])
            self.patch(seed, ("catalog.py", "billing.py"))
            self.assertFalse(verify_patch(seed)["passed"])

    def test_all_module_fixes_and_integration_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.copy_fixture(tmp)
            self.patch(project, ("catalog.py", "billing.py", "shipping.py"))
            self.assertTrue(verify_patch(project)["passed"])

    def test_verifier_and_answer_key_stay_outside_writable_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.copy_fixture(tmp)
            self.assertFalse((project / "checks").exists())
            self.assertFalse((project / "patch.py").exists())
            self.assertFalse((project / "triage.json").exists())


if __name__ == "__main__":
    unittest.main()
