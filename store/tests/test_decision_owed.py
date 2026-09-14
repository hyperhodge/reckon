#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""`decision_owed` -- what is waiting on a person. An earlier session.

Usage: python3 -m unittest discover -s store/tests

Own fixtures in a temp dir. Nothing here reads ~/.claude, the real archive or
a binary; the one class that reads the real store only reads it.

THE TEST THIS FILE EXISTS FOR is `test_a_build_task_awaiting_approval_is_not_a
_decision`, and it is a test about a definition rather than about code. The
field was added because the owner could not find, in a hundred-line task record,
the one sentence saying what he had to decide. The failure mode is not that it
breaks -- it is that a later session marks every drafted task as owing a
decision, at which point `--owed` is `--list tasks` with more words and the
register is unreadable again in exactly the same way.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import governance       # noqa: E402
import store as S       # noqa: E402


def task_fields(**overrides):
    fields = {"ref": "P-0001-T19", "project_ref": "P-0001", "task_class": "decision",
              "task_type": "design", "state": "drafted", "brief": "A brief",
              "decision_owed": None, "runs": []}
    fields.update(overrides)
    return fields


class SettingIt(unittest.TestCase):
    """NOTE: two fixtures here were rewritten on 7 September because they were
    themselves choices -- "Say yes ... or overrule it". The tests encoded the
    malformed shape a whole session had been writing, which is why the audit
    test at the bottom of this file runs against the REAL records and not
    against fixtures a session chose for itself.
    """


    def test_it_sets_and_squeezes(self):
        fields = S.set_decision_owed(task_fields(), "  Add the field\n  to rails.json.  ")
        self.assertEqual(fields["decision_owed"], "Add the field to rails.json.")

    def test_it_does_not_mutate_the_input(self):
        original = task_fields()
        S.set_decision_owed(original, "Decide.")
        self.assertIsNone(original["decision_owed"])

    def test_empty_text_is_refused(self):
        with self.assertRaises(S.StoreError):
            S.set_decision_owed(task_fields(), "   ")

    def test_a_paragraph_is_refused(self):
        """The cap is the feature. The long form belongs in the body, which is
        exactly where it was when nobody could find it."""
        with self.assertRaises(S.StoreError) as caught:
            S.set_decision_owed(task_fields(), "word " * 100)
        self.assertIn("ONE SENTENCE", str(caught.exception))

    def test_the_cap_is_generous_enough_for_a_real_decision(self):
        real = ("Add a second field to rails.json, overflow: subscription | credit "
                "| unestablished, alongside the existing rail field.")
        self.assertEqual(S.set_decision_owed(task_fields(), real)["decision_owed"], real)


class ClearingIt(unittest.TestCase):

    def test_clearing_nulls_it(self):
        fields, _ = S.clear_decision_owed(task_fields(decision_owed="Decide."))
        self.assertIsNone(fields["decision_owed"])

    def test_clearing_without_an_outcome_warns(self):
        """This field is the question and `outcome` is the answer. Clearing
        without one deletes the question and records nothing."""
        _, warning = S.clear_decision_owed(task_fields(decision_owed="Decide."))
        self.assertIsNotNone(warning)
        self.assertIn("--outcome", warning)

    def test_clearing_with_an_outcome_does_not_warn(self):
        _, warning = S.clear_decision_owed(
            task_fields(decision_owed="Decide.", outcome="2026-09-07: Decided."))
        self.assertIsNone(warning)

    def test_clearing_does_not_touch_the_outcome(self):
        fields, _ = S.clear_decision_owed(
            task_fields(decision_owed="Decide.", outcome="2026-09-07: Decided."))
        self.assertEqual(fields["outcome"], "2026-09-07: Decided.")


class TheView(unittest.TestCase):

    def store_with(self, tasks):
        return {"projects": {}, "tasks": {ref: {"fields": f} for ref, f in tasks.items()},
                "runs": [], "decisions": [], "root": "."}

    def test_only_tasks_carrying_one_appear(self):
        owed = S.decisions_owed(self.store_with({
            "P-0001-T19": task_fields(decision_owed="Decide the schema."),
            "P-0001-T22": task_fields(ref="P-0001-T22"),
        }))
        self.assertEqual([item["ref"] for item in owed], ["P-0001-T19"])

    def test_a_build_task_awaiting_approval_is_not_a_decision(self):
        """THE TEST THIS FILE EXISTS FOR. `drafted` already says a task is
        waiting to be approved. If that alone put a task in this view, the view
        would be `--list tasks` again and the field would have bought nothing."""
        owed = S.decisions_owed(self.store_with({
            "P-0001-T22": task_fields(ref="P-0001-T22", state="drafted"),
            "P-0001-T23": task_fields(ref="P-0001-T23", state="drafted"),
        }))
        self.assertEqual(owed, [])

    def test_it_is_ref_sorted_and_carries_what_the_reader_needs(self):
        owed = S.decisions_owed(self.store_with({
            "P-0001-T25": task_fields(ref="P-0001-T25", decision_owed="B."),
            "P-0001-T17": task_fields(ref="P-0001-T17", decision_owed="A.",
                                      brief="Name the thing"),
        }))
        self.assertEqual([item["ref"] for item in owed], ["P-0001-T17", "P-0001-T25"])
        self.assertEqual(owed[0]["decision_owed"], "A.")
        self.assertEqual(owed[0]["brief"], "Name the thing")
        self.assertEqual(owed[0]["state"], "drafted")

    def test_it_computes_nothing(self):
        """A view, not a check: it reports what a person wrote down."""
        owed = S.decisions_owed(self.store_with({
            "P-0001-T19": task_fields(decision_owed="Verbatim, unchanged.")}))
        self.assertEqual(owed[0]["decision_owed"], "Verbatim, unchanged.")


class TheChecker(unittest.TestCase):

    def setUp(self):
        self.register = S.RefRegister(path=Path(tempfile.mkdtemp()) / "refs.json")

    def store_with(self, tasks):
        return {"projects": {"P-0001": {"fields": {"ref": "P-0001", "stage": "prototype"}}},
                "tasks": {ref: {"fields": f} for ref, f in tasks.items()},
                "runs": [], "decisions": [], "root": "."}

    def test_a_shipped_task_still_owing_a_decision_is_an_advisory(self):
        result = S.check(self.store_with({
            "P-0001-T19": task_fields(state="shipped", decision_owed="Decide.")}),
            self.register)
        self.assertTrue(any("decision_owed" in a for a in result["advisories"]))

    def test_a_drafted_task_owing_a_decision_is_not(self):
        result = S.check(self.store_with({
            "P-0001-T19": task_fields(state="drafted", decision_owed="Decide.")}),
            self.register)
        self.assertFalse(any("decision_owed" in a for a in result["advisories"]))

    def test_it_is_never_a_problem(self):
        """Advisory throughout. A decision nobody has taken is not a defect."""
        result = S.check(self.store_with({
            "P-0001-T19": task_fields(state="shipped", decision_owed="Decide.")}),
            self.register)
        self.assertFalse(any("decision_owed" in p for p in result["problems"]))


class Disclosive(unittest.TestCase):
    """The RI-08 shape: a disclosive field that ships one session ahead of its
    redaction. An earlier session found three, `parked_ref` was a fourth on 7 September,
    and this one is redacted in the same change that adds it."""

    def test_it_is_a_disclosive_field(self):
        self.assertIn("decision_owed", S.DISCLOSIVE_FIELDS)

    def test_the_redactor_replaces_it(self):
        payload = {"projects": {}, "tasks": {"P-0001-T19": task_fields(
            decision_owed="Decide whether to keep billing Acme Ltd monthly.")}}
        self.assertNotIn("Acme", json.dumps(governance.present(payload)))

    def test_an_unredacted_one_fails_the_audit(self):
        """The audit has to be able to fail, or it is decoration."""
        payload = {"projects": {}, "tasks": {"P-0001-T19": task_fields(
            decision_owed="Decide whether to keep billing Acme Ltd monthly.")}}
        audit = governance.audit_presentation(payload, {"projects": {}, "tasks":
                                                        dict(payload["tasks"])})
        self.assertFalse(audit["clean"])

    def test_the_audit_passes_on_the_redacted_payload(self):
        payload = {"projects": {}, "tasks": {"P-0001-T19": task_fields(
            decision_owed="Decide whether to keep billing Acme Ltd monthly.")}}
        audit = governance.audit_presentation(payload, governance.present(payload))
        self.assertTrue(audit["clean"], audit["leaked"])

    def test_it_is_counted_in_the_export_manifest(self):
        payload = {"projects": {}, "tasks": {"P-0001-T19": task_fields(
            decision_owed="Decide.")}}
        self.assertEqual(S.disclosive_inventory(payload).get("decision_owed"), 1)


class OnDisk(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "P-0001-T19.md"
        self.addCleanup(self.dir.cleanup)

    def test_it_round_trips(self):
        S.write_task_record(self.path, S.set_decision_owed(task_fields(), "Decide it."))
        fields, _ = S.read_record(self.path)
        self.assertEqual(fields["decision_owed"], "Decide it.")

    def test_a_cleared_one_round_trips_as_null(self):
        S.write_task_record(self.path, S.set_decision_owed(task_fields(), "Decide it."))
        fields, body = S.read_record(self.path)
        cleared, _ = S.clear_decision_owed(fields)
        S.write_task_record(self.path, cleared, body)
        fields, _ = S.read_record(self.path)
        self.assertIsNone(fields["decision_owed"])

    def test_setting_it_cannot_drop_an_outcome(self):
        """The append-only door still guards the write."""
        S.write_task_record(self.path, task_fields(outcome="2026-09-07: One."))
        with self.assertRaises(S.AppendOnlyViolation):
            S.write_task_record(self.path, S.set_decision_owed(task_fields(), "Decide."))


if __name__ == "__main__":
    unittest.main()


class OnePropositionNotAChoice(unittest.TestCase):
    """The owner's rule, 7 September 2026, and the failure that produced it.

    P-0001-T26 asked him to choose between rewriting the README and retiring
    it. He pressed Reject and wrote "Keep it for now, I want both" -- a third
    option neither branch offered, filed as a rejection of a question that
    cannot be rejected. Five of the seven open questions had the same fault.
    """

    def test_a_choice_is_refused(self):
        with self.assertRaises(S.StoreError) as caught:
            S.set_decision_owed(task_fields(), "Rewrite the README, or retire it.")
        self.assertIn("CHOICE", str(caught.exception))

    def test_whether_is_refused_too(self):
        with self.assertRaises(S.StoreError):
            S.set_decision_owed(task_fields(), "Say whether the sweep runs first.")

    def test_a_single_proposition_is_accepted(self):
        text = "Rewrite README.md around the app, keeping it as a document."
        self.assertEqual(S.set_decision_owed(task_fields(), text)["decision_owed"], text)

    def test_the_word_must_stand_alone(self):
        """'or' inside a word is not a choice. 'Record the outcome' must pass."""
        text = "Record the outcome and ratify the coordinator before shipping."
        self.assertEqual(S.set_decision_owed(task_fields(), text)["decision_owed"], text)

    def test_every_open_question_in_the_real_store_is_a_proposition(self):
        """The audit, against the records on disk rather than a fixture."""
        for item in S.decisions_owed(S.load_store(ROOT)):
            padded = f" {item['decision_owed'].lower()} "
            for word in (" or ", " whether "):
                self.assertNotIn(word, padded, item["ref"])
