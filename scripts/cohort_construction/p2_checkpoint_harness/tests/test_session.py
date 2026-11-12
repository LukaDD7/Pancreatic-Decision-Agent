"""Session log: append-only, projection, and compaction surface replacement."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.cohort_construction.p2_checkpoint_harness import session as S


def _session(directory, session_id="t1"):
    return S.Session(Path(directory) / "session.jsonl", session_id=session_id,
                     metadata=dict(case_id="TEST-01"))


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_HSL001_projection_follows_append_order(self):
        session = _session(self.dir)
        session.append(S.SYSTEM_MESSAGE, dict(text="sys"))
        session.append(S.USER_MESSAGE, dict(text="u1"))
        session.append(S.ASSISTANT_MESSAGE, dict(text="a1"))
        messages = session.derive_messages()
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])
        session.close()

    def test_HSL002_unknown_event_type_rejected(self):
        session = _session(self.dir)
        with self.assertRaises(ValueError):
            session.append("made/up", dict())
        session.close()

    def test_HSL003_existing_nonempty_session_refused(self):
        path = self.dir / "session.jsonl"
        path.write_text("already here\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            S.Session(path, session_id="t1")

    def test_HSL004_resume_rejects_foreign_session_id(self):
        session = _session(self.dir, session_id="aaa")
        session.append(S.USER_MESSAGE, dict(text="x"))
        session.close()
        with self.assertRaises(ValueError):
            S.Session(self.dir / "session.jsonl", session_id="bbb", resume=True)

    def test_HSL005_surface_op_hides_range_but_log_keeps_it(self):
        session = _session(self.dir)
        session.append(S.SYSTEM_MESSAGE, dict(text="sys"))
        first = session.append(S.USER_MESSAGE, dict(text="old"))
        last = session.append(S.TOOL_RESULT, dict(tool_call_id="c1", content="r"))
        session.append(S.USER_MESSAGE, dict(
            text="SUMMARY", surfaceOp=dict(op="replace", startSeq=first, endSeq=last)))
        messages = session.derive_messages()
        self.assertEqual([m["content"] for m in messages], ["sys", "SUMMARY"])
        # The shadowed events are still readable from the log.
        self.assertEqual(session.get(first)["payload"]["text"], "old")
        self.assertEqual(session.get(last)["payload"]["content"], "r")
        session.close()

    def test_HSL006_compaction_may_not_shadow_system_head(self):
        session = _session(self.dir)
        head = session.append(S.SYSTEM_MESSAGE, dict(text="sys"))
        second = session.append(S.USER_MESSAGE, dict(text="u"))
        with self.assertRaises(ValueError):
            session.validate_range(head, second)
        session.close()

    def test_HSL007_unmatched_start_is_reported_as_orphan(self):
        session = _session(self.dir)
        session.append(S.COMPACTION_START, dict(compaction_id="c1"))
        with self.assertRaises(S.CompactionLocked):
            session.assert_compaction_idle()
        self.assertEqual([session.compaction_id(record)
                          for record in session.abandoned_compactions()], ["c1"])
        session.abandon_stale_compaction("c1", reason="crash during summary")
        self.assertIsNone(session.active_compaction())
        the_end = session.last(S.COMPACTION_END)
        self.assertTrue(the_end["payload"]["abandoned"])
        session.close()

    def test_HSL008_closed_compaction_releases_lock(self):
        session = _session(self.dir)
        session.append(S.COMPACTION_START, dict(compaction_id="c1"))
        session.append(S.COMPACTION_END, dict(compaction_id="c1", outcome="COMPLETE"))
        self.assertIsNone(session.active_compaction())
        session.assert_compaction_idle()
        session.close()

    def test_HSL009_log_is_jsonl_with_replayable_header(self):
        session = _session(self.dir)
        session.append(S.USER_MESSAGE, dict(text="甲"))
        session.close()
        lines = (self.dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["session_id"], "t1")
        self.assertEqual(json.loads(lines[1])["type"], S.USER_MESSAGE)
        self.assertEqual(json.loads(lines[1])["payload"]["text"], "甲")

    def test_HSL010_append_after_close_rejected(self):
        session = _session(self.dir)
        session.close()
        with self.assertRaises(S.SessionClosed):
            session.append(S.USER_MESSAGE, dict(text="late"))

    def test_HSL011_tool_history_is_projected_in_wire_shape(self):
        # The provider resolves a role=tool message by name and call id, and
        # wants tool_calls[].type + function.arguments as a string. The internal
        # shape was sent verbatim and refused with HTTP 400 on the first live
        # run (2026-11-09).
        session = _session(self.dir)
        session.append(S.USER_MESSAGE, dict(text="甲"))
        session.append(S.ASSISTANT_MESSAGE, dict(
            text="", tool_calls=[dict(id="call_1", name="lookup",
                                      arguments=dict(q="甲"))]))
        session.append(S.TOOL_RESULT, dict(tool_call_id="call_1", name="lookup",
                                           content='{"ok": true}', is_error=False))
        roles = [m["role"] for m in session.derive_messages()]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        assistant = session.derive_messages()[1]
        call = assistant["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "lookup")
        # Arguments are a JSON string on the wire, and they round-trip.
        self.assertEqual(json.loads(call["function"]["arguments"]), {"q": "甲"})
        tool_message = session.derive_messages()[2]
        self.assertEqual(tool_message["name"], "lookup")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        # The log keeps the internal shape; only the projection is translated.
        logged = [record for record in session.events()
                  if record["type"] == S.ASSISTANT_MESSAGE][0]
        self.assertIsInstance(logged["payload"]["tool_calls"][0]["arguments"], dict)
        session.close()


if __name__ == "__main__":
    unittest.main()