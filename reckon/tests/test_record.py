# SPDX-License-Identifier: Apache-2.0
"""Tests for reckon/record.py: recorder.py, then the store's run intake --
new / corrected / unchanged (RECKON-1.1-SPEC.md R1)."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reckon import record  # noqa: E402


class FakeRecorder:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def main(self, argv):
        self.calls.append(list(argv))
        return self.rc


class TestRecordRun(unittest.TestCase):
    def test_passes_source_and_session_through_to_recorder_unchanged(self):
        fake = FakeRecorder()
        with mock.patch.object(record, "_recorder", return_value=fake), \
             mock.patch.object(record, "_store",
                               side_effect=AssertionError("store touched in --session mode")):
            record.run(source="/tmp/x", session="abc12345", out=io.StringIO())
        self.assertEqual(fake.calls, [["--source", "/tmp/x", "--session", "abc12345"]])

    def test_session_mode_prints_a_note_and_does_not_touch_the_store(self):
        fake = FakeRecorder()
        out = io.StringIO()
        with mock.patch.object(record, "_recorder", return_value=fake), \
             mock.patch.object(record, "_store",
                               side_effect=AssertionError("store touched in --session mode")):
            rc = record.run(session="abc12345", out=out)
        self.assertEqual(rc, 0)
        self.assertIn("--session", out.getvalue())

    def test_a_recorder_failure_stops_before_the_store_is_touched(self):
        fake = FakeRecorder(rc=2)
        with mock.patch.object(record, "_recorder", return_value=fake), \
             mock.patch.object(record, "_store",
                               side_effect=AssertionError("store touched after a failure")):
            rc = record.run(out=io.StringIO())
        self.assertEqual(rc, 2)

    def test_success_ingests_and_reports_new_corrected_and_unchanged(self):
        fake_recorder = FakeRecorder(rc=0)
        fake_register = mock.Mock()
        fake_store = mock.Mock()
        fake_store.RefRegister.load.return_value = fake_register
        fake_store.load_store.return_value = {"runs": []}
        appended = [{"started_at": "2026-09-15T00:00:00Z", "ref": "R-0001"}]
        report = {"new": [{"ref": "R-0001", "session": "abcdefgh12", "cost_usd": 1.23}],
                 "superseded": [{"ref": "R-0002", "from_cost_usd": 1.0, "to_cost_usd": 1.5,
                                 "changed": ["cost_usd"]}],
                 "unchanged": ["R-0003"]}
        fake_store.ingest_runs.return_value = (appended, report)
        fake_store.runs_path.return_value = Path("/tmp/fake-runs/2026-09.jsonl")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ledger").mkdir()
            (root / "ledger" / "runs.json").write_text(json.dumps({"runs": []}),
                                                        encoding="utf-8")
            (root / "store").mkdir()
            out = io.StringIO()
            with mock.patch.object(record, "_recorder", return_value=fake_recorder), \
                 mock.patch.object(record, "_store", return_value=fake_store):
                rc = record.run(root=root, out=out)

        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("new        R-0001", text)
        self.assertIn("corrected  R-0002", text)
        self.assertIn("unchanged  1", text)
        fake_store.append_runs.assert_called_once_with(fake_store.runs_path.return_value,
                                                        appended)
        fake_register.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
