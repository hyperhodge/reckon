#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s store/tests

Task capture, spec-core.md Item 1. On 10 September a session minted a task ref
by hand and saved an empty register over store/refs.json, losing 84 bindings
(P-0001-T46). This command is the fix, so these tests are the guarantee. Every
fixture is a temporary directory; nothing here reads or writes the real store.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import store  # noqa: E402


class CaptureTaskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for folder in ("projects", "tasks", "runs", "store"):
            (self.root / folder).mkdir()
        self.refs = self.root / "store" / "refs.json"
        register = store.RefRegister(path=self.refs)
        register.allocate_project("the project")                      # P-0001
        register.adopt("P-0001-T01", "an-existing-task", "task")
        register.save()
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "name": "the project",
                            "one_liner": "the project", **store.CAPTURE_DEFAULTS})

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv):
        out = io.StringIO()
        code = store.main(["--root", str(self.root), *argv], out=out)
        return code, out.getvalue()

    def bindings(self):
        return json.loads(self.refs.read_text(encoding="utf-8"))["bindings"]


class TestItAllocatesAndWrites(CaptureTaskCase):
    def test_it_allocates_the_next_number_and_writes_the_file(self):
        code, text = self.main("--capture-task", "P-0001", "a new task")
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines()[0], "P-0001-T02")
        path = self.root / "tasks" / "P-0001-T02.md"
        self.assertTrue(path.exists())
        fields, body = store.read_record(path)
        self.assertEqual(fields["ref"], "P-0001-T02")
        self.assertEqual(fields["project_ref"], "P-0001")
        self.assertEqual(fields["brief"], "a new task")
        self.assertEqual(fields["state"], "drafted")
        self.assertIn("a new task", body)
        self.assertEqual(self.bindings()["P-0001-T02"]["kind"], "task")

    def test_two_captures_in_a_row_get_consecutive_numbers(self):
        _, first = self.main("--capture-task", "P-0001", "first")
        _, second = self.main("--capture-task", "P-0001", "second")
        self.assertEqual(first.splitlines()[0], "P-0001-T02")
        self.assertEqual(second.splitlines()[0], "P-0001-T03")

    def test_a_number_is_never_reused_even_when_its_file_is_gone(self):
        self.main("--capture-task", "P-0001", "first")
        (self.root / "tasks" / "P-0001-T02.md").unlink()
        _, text = self.main("--capture-task", "P-0001", "second")
        self.assertEqual(text.splitlines()[0], "P-0001-T03")

    def test_the_captured_record_passes_the_store_check(self):
        self.main("--capture-task", "P-0001", "a new task")
        result = store.check(store.load_store(self.root), store.RefRegister.load(self.refs))
        self.assertEqual([p for p in result["problems"] if "T02" in p], [])


class TestItRefuses(CaptureTaskCase):
    def assert_nothing_written(self, before):
        self.assertEqual(self.refs.read_text(encoding="utf-8"), before)
        self.assertEqual(list((self.root / "tasks").glob("*.md")), [])

    def test_an_unknown_project_is_refused_and_nothing_is_written(self):
        before = self.refs.read_text(encoding="utf-8")
        code, text = self.main("--capture-task", "P-0099", "a task")
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assert_nothing_written(before)

    def test_a_malformed_project_ref_is_refused(self):
        before = self.refs.read_text(encoding="utf-8")
        code, _ = self.main("--capture-task", "P-1", "a task")
        self.assertEqual(code, 1)
        self.assert_nothing_written(before)

    def test_an_empty_one_liner_is_refused_and_nothing_is_written(self):
        before = self.refs.read_text(encoding="utf-8")
        for empty in ("", "   ", "--- !!"):
            code, text = self.main("--capture-task", "P-0001", empty)
            self.assertEqual(code, 1, empty)
            self.assertIn("REFUSED", text)
        self.assert_nothing_written(before)

    def test_wording_bound_under_another_project_is_refused(self):
        register = store.RefRegister.load(self.refs)
        register.allocate_project("another")                           # P-0002
        register.allocate_task("P-0002", "shared-wording")
        register.save()
        before = self.refs.read_text(encoding="utf-8")
        code, text = self.main("--capture-task", "P-0001", "Shared wording")
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assert_nothing_written(before)


class TestItNeverOverwrites(CaptureTaskCase):
    def test_an_unregistered_file_at_the_next_ref_is_not_overwritten(self):
        """A hand-made file the register never heard of sits where the next ref
        would go. It survives byte for byte and the register is not saved."""
        existing = self.root / "tasks" / "P-0001-T02.md"
        existing.write_text("---\nref: P-0001-T02\n---\n\nhand made\n", encoding="utf-8")
        before = self.refs.read_text(encoding="utf-8")
        code, text = self.main("--capture-task", "P-0001", "a new task")
        self.assertEqual(code, 1)
        self.assertIn("exists", text)
        self.assertEqual(existing.read_text(encoding="utf-8"),
                         "---\nref: P-0001-T02\n---\n\nhand made\n")
        self.assertEqual(self.refs.read_text(encoding="utf-8"), before)

    def test_the_same_wording_twice_does_not_overwrite_the_first(self):
        self.main("--capture-task", "P-0001", "a new task")
        path = self.root / "tasks" / "P-0001-T02.md"
        original = path.read_text(encoding="utf-8")
        code, text = self.main("--capture-task", "P-0001", "A new task!")
        self.assertEqual(code, 1)
        self.assertIn("exists", text)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(len(list((self.root / "tasks").glob("*.md"))), 1)


class TestTheRegisterKeepsEveryBinding(CaptureTaskCase):
    def test_every_previous_binding_survives_on_disk(self):
        """The 10 September loss, tested directly: after captures and refusals
        the file on disk holds every binding it held before, unchanged."""
        before = self.bindings()
        self.main("--capture-task", "P-0001", "first")
        self.main("--capture-task", "P-0099", "refused")
        self.main("--capture-task", "P-0001", "second")
        after = self.bindings()
        for ref, binding in before.items():
            self.assertEqual(after.get(ref), binding, ref)
        self.assertEqual(len(after), len(before) + 2)


if __name__ == "__main__":
    unittest.main()
