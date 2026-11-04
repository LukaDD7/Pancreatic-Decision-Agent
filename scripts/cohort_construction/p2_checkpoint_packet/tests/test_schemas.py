"""Check the published schemas with an independent standard validator if available."""

import json
import unittest

from scripts.cohort_construction.p2_checkpoint_packet.compiler import SCHEMA_DIR
from scripts.cohort_construction.p2_checkpoint_packet.tests import test_compiler as fixtures

try:
    import jsonschema
except ImportError:
    jsonschema = None


@unittest.skipIf(jsonschema is None, "Optional independent JSON Schema validator is not installed")
class PublishedSchemaTests(unittest.TestCase):
    def test_every_schema_is_valid_draft_2020_12(self):
        for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
            with self.subTest(schema=path.name):
                jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))

    def test_actual_api_objects_match_published_schemas(self):
        source_manifest, compiled = fixtures.manifest(), fixtures.safe_compiled()
        values = {"SourceManifest": source_manifest, "SplitProposal": fixtures.proposal(source_manifest, []),
                  "CompiledPartition": compiled, "SemanticAudit": fixtures.semantic(compiled),
                  "FrozenQuestionnaire": fixtures.frozen_questionnaire(), "FrozenCheckpointPacket": fixtures.packet(),
                  "ReviewSubmission": fixtures.SubmissionTests().submission()}
        for name, value in values.items():
            with self.subTest(contract=name):
                schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator(schema).validate(value)


if __name__ == "__main__":
    unittest.main()
