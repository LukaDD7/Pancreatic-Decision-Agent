import contextlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.cohort_construction.p2_checkpoint_packet.__main__ import main
from scripts.cohort_construction.p2_checkpoint_packet.tests import test_compiler as fixtures
from scripts.cohort_construction.p2_checkpoint_packet.tests import test_semantic_audit as semantic_fixtures


class CliAndRegistryTests(unittest.TestCase):
    def test_cli_preserves_failed_audit_and_refuses_overwrite(self):
        source_manifest = fixtures.manifest("甲乙")
        incomplete = fixtures.proposal(source_manifest, [fixtures.quoted("s1", "KEEP", "甲")])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
            (folder / "proposal.json").write_text(json.dumps(incomplete), encoding="utf-8")
            args = ["compile", "--manifest", str(folder / "manifest.json"), "--proposal", str(folder / "proposal.json"),
                    "--output", str(folder / "compiled.json")]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 3)
                before = (folder / "compiled.json").read_bytes()
                self.assertEqual(main(args), 2)
                self.assertEqual((folder / "compiled.json").read_bytes(), before)
            result = json.loads(before)
            self.assertEqual(result["mechanical_audit"]["status"], "FAIL")
            self.assertEqual(result["sources"][0]["segments"][-1]["disposition"], "REVIEW")

    def test_all_registered_required_tests_exist_and_cover_core_counterexamples(self):
        registry = json.loads((Path(__file__).resolve().parents[1] / "regression_issue_map.json").read_text(encoding="utf-8"))
        registered = set()
        ids = set()
        for issue in registry["issues"]:
            self.assertNotIn(issue["issue_id"], ids)
            ids.add(issue["issue_id"])
            self.assertTrue(issue["required_tests"])
            for name in issue["required_tests"]:
                module_name, class_name, test_name = name.rsplit(".", 2)
                module = importlib.import_module(module_name)
                self.assertTrue(callable(getattr(getattr(module, class_name), test_name)))
                registered.add(name)
        actual = set()
        for module, names in ((fixtures, ("ExactSourceTests", "AnchorTests", "PartitionTests", "BindingTests", "DoctorIsolationTests", "SubmissionTests")),
                              (semantic_fixtures, ("SemanticAuditTests",))):
            for name in names:
                cls = getattr(module, name)
                for test_name in unittest.defaultTestLoader.getTestCaseNames(cls):
                    actual.add(f"{module.__name__}.{name}.{test_name}")
        self.assertEqual(actual, registered)


if __name__ == "__main__":
    unittest.main()
