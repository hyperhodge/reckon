#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s store/tests

Capture (§3.1) -- two fields and eight seconds. The thing worth protecting is
that a ref is ALLOCATED and never minted: the front end queues text and this is
the only door a Project comes through, so a ref invented anywhere else is a ref
spent twice. Every fixture is a temporary directory; nothing here reads the real
store, ~/.claude, the archive or the network.
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import store  # noqa: E402


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects").mkdir()
        (self.root / "store").mkdir()
        self.register = store.RefRegister(path=self.root / "store" / "refs.json")

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, one_liner, name=None):
        return store.capture_project(one_liner, self.register, name=name, root=self.root)


class TestCaptureAllocatesThroughTheStore(CaptureCase):
    def test_a_capture_allocates_a_ref_and_writes_a_record(self):
        ref, path, debts, created = self.capture("a thing worth trying")
        self.assertEqual(ref, "P-0001")
        self.assertTrue(created)
        self.assertTrue(path.exists())
        fields, _ = store.read_record(path)
        self.assertEqual(fields["ref"], "P-0001")
        self.assertEqual(fields["one_liner"], "a thing worth trying")
        self.assertEqual(fields["stage"], "captured")

    def test_the_ref_is_recorded_in_the_allocation_register(self):
        """Allocation is a WRITE, not a derivation. That is the one difference
        that lets a store mint a sequential ref where the Recorder cannot."""
        ref, _, _, _ = self.capture("a thing")
        self.assertIn(ref, self.register.data["bindings"])
        self.assertEqual(self.register.data["bindings"][ref]["kind"], "project")

    def test_refs_run_on_and_are_never_reused(self):
        a, _, _, _ = self.capture("first")
        b, _, _, _ = self.capture("second")
        c, _, _, _ = self.capture("third")
        self.assertEqual([a, b, c], ["P-0001", "P-0002", "P-0003"])

    def test_capturing_the_same_thing_twice_returns_the_same_ref(self):
        """Idempotent on the slug. Two refs for one project is the expensive
        mistake, because a ref is never reused and never renumbered."""
        a, path_a, _, created_a = self.capture("build the thing", name="The Thing")
        b, path_b, _, created_b = self.capture("build the thing again", name="the  thing")
        self.assertEqual(a, b)
        self.assertEqual(path_a, path_b)
        self.assertTrue(created_a)
        self.assertFalse(created_b)

    def test_a_second_capture_does_not_overwrite_the_record_on_disk(self):
        _, path, _, _ = self.capture("original wording", name="Same")
        self.capture("completely different wording", name="Same")
        fields, _ = store.read_record(path)
        self.assertEqual(fields["one_liner"], "original wording")

    def test_it_continues_from_refs_already_allocated(self):
        self.register.allocate_project("something else")   # takes P-0001
        ref, _, _, _ = self.capture("a thing")
        self.assertEqual(ref, "P-0002")


class TestCaptureIsTwoFieldsAndGatesNothing(CaptureCase):
    def test_a_capture_with_no_one_liner_is_refused(self):
        """The one field that cannot be defaulted. This is not a stage gate --
        it is the record having no content at all."""
        with self.assertRaises(store.StoreError):
            self.capture("   ")

    def test_the_name_defaults_to_the_one_liner(self):
        _, path, _, _ = self.capture("a short idea")
        fields, _ = store.read_record(path)
        self.assertEqual(fields["name"], "a short idea")

    def test_a_long_one_liner_does_not_become_a_long_name(self):
        _, path, _, _ = self.capture("x " * 100)
        fields, _ = store.read_record(path)
        self.assertLessEqual(len(fields["name"]), 60)

    def test_capture_reports_what_the_stage_owes_and_refuses_nothing(self):
        """§3.1: fields become mandatory as a project advances, NEVER at
        creation. A capture owing nothing but name and one_liner is correct."""
        ref, path, debts, _ = self.capture("a thing")
        self.assertEqual(debts, [], "captured owes only name and one_liner, both given")
        fields, _ = store.read_record(path)
        self.assertEqual(store.stage_debt(fields), [])

    def test_governance_fields_are_present_and_null_rather_than_absent(self):
        """stage_debt() can only report a debt on a field it can see, and the
        Record screen can only show an empty field it was given."""
        _, path, _, _ = self.capture("a thing")
        fields, _ = store.read_record(path)
        for key in ("context", "type", "is_ai_system", "my_role", "act_tier",
                    "inherent_risk", "residual_risk", "next_review", "cost_to_date_gbp"):
            self.assertIn(key, fields)
            self.assertIsNone(fields[key])

    def test_a_captured_project_owes_its_way_forward_and_is_now_stopped(self):
        """Capture is still two fields (the test above). What changed in session
        6 is what happens on the way out: advancing to `scoped` is refused until
        the debts are filled, which is §3.1's principle enforced rather than
        merely stated."""
        _, path, _, _ = self.capture("a thing")
        fields, _ = store.read_record(path)
        with self.assertRaises(store.StageGateRefused) as caught:
            store.transition_project(fields, "scoped")
        owed = {d["field"] for d in caught.exception.debts}
        self.assertIn("context", owed)
        self.assertIn("directory", owed)

    def test_capture_itself_is_never_gated(self):
        """The other half of §3.1, and the half enforcement must not break."""
        ref, path, debts, created = self.capture("a shower thought")
        self.assertTrue(created)
        self.assertTrue(Path(path).exists())
        self.assertEqual(debts, [], "`captured` owes exactly the two fields capture "
                                    "supplies -- that is what eight seconds buys")
        fields, _ = store.read_record(path)
        self.assertTrue(store.stage_debt(fields, "scoped"),
                        "and it owes plenty at the next stage, which is the gate")


class TestTheRecordStaysReadable(CaptureCase):
    def test_the_record_round_trips_through_the_launcher_parser(self):
        """write_record parses back what it wrote, so a captured project is
        readable by wrapper/launch.py by construction rather than by hope."""
        _, path, _, _ = self.capture("a thing, with a comma: and a colon")
        fields, body = store.read_record(path)
        self.assertEqual(fields["one_liner"], "a thing, with a comma: and a colon")
        self.assertIn(fields["ref"], body)

    def test_newlines_are_squeezed_out_of_the_one_liner(self):
        """Front matter here is flat scalars and inline lists only. A newline
        would make the file unreadable to the launcher's parser."""
        _, path, _, _ = self.capture("a thing\nover two lines")
        fields, _ = store.read_record(path)
        self.assertNotIn("\n", fields["one_liner"])


class TestTheCLI(CaptureCase):
    def test_capture_via_main_writes_the_record_and_saves_the_register(self):
        import io
        out = io.StringIO()
        (self.root / "tasks").mkdir()
        (self.root / "runs").mkdir()
        code = store.main(["--root", str(self.root), "--capture", "a thing"], out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("captured", text)
        self.assertIn("P-0001", text)
        self.assertTrue((self.root / "projects" / "P-0001.md").exists())
        self.assertTrue((self.root / "store" / "refs.json").exists())

    def test_capturing_twice_via_main_says_so_and_writes_nothing_new(self):
        import io
        (self.root / "tasks").mkdir()
        (self.root / "runs").mkdir()
        store.main(["--root", str(self.root), "--capture", "a thing"], out=io.StringIO())
        out = io.StringIO()
        store.main(["--root", str(self.root), "--capture", "a thing"], out=out)
        self.assertIn("exists", out.getvalue())
        self.assertEqual(len(list((self.root / "projects").glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
