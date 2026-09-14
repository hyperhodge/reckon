#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""§2.2's `outcome` field, and the plan in the register. TASK-016.

Usage: python3 -m unittest discover -s store/tests

Own fixtures in a temp dir. Nothing here reads ~/.claude, the real archive or
the real store, and nothing here can reach a binary.

The test this file exists for is `test_a_write_that_replaces_is_refused`. An
outcome that can be overwritten is a Status column with extra steps, and the
whole reason PLAN.md was worth retiring is that its narrative accumulated --
each session's paragraph beside the last one rather than on top of it. The
append discipline is the feature, so it is the thing under test.
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
    fields = {"ref": "P-0001-T03", "project_ref": "P-0001", "task_class": "code-task",
              "task_type": "execute", "state": "built", "brief": "A brief",
              "approved_by": "ada", "approved_at": "2026-09-05T09:00:00Z",
              "runs": []}
    fields.update(overrides)
    return fields


class OutcomeAppends(unittest.TestCase):
    """Deliverable 1: the field, and the discipline that is the point of it."""

    def test_first_entry_is_datestamped(self):
        fields, entry = S.append_outcome(task_fields(), "Shipped the wrapper.",
                                         at="2026-09-05T18:00:00Z")
        self.assertEqual(entry, "2026-09-05: Shipped the wrapper.")
        self.assertEqual(fields["outcome"], "2026-09-05: Shipped the wrapper.")

    def test_a_second_entry_sits_beside_the_first_not_on_top_of_it(self):
        fields, _ = S.append_outcome(task_fields(), "What shipped.",
                                     at="2026-09-05T18:00:00Z")
        fields, _ = S.append_outcome(fields, "What did not.",
                                     at="2026-09-06T18:00:00Z")
        self.assertEqual(S.outcome_entries(fields["outcome"]),
                         ["2026-09-05: What shipped.", "2026-09-06: What did not."])

    def test_the_original_is_still_there_verbatim(self):
        first, _ = S.append_outcome(task_fields(), "One.", at="2026-09-05T00:00:00Z")
        second, _ = S.append_outcome(first, "Two.", at="2026-09-06T00:00:00Z")
        self.assertTrue(second["outcome"].startswith(first["outcome"]))

    def test_the_source_record_is_not_mutated(self):
        original = task_fields()
        S.append_outcome(original, "One.", at="2026-09-05T00:00:00Z")
        self.assertNotIn("outcome", original)

    def test_newlines_are_folded_so_front_matter_stays_readable(self):
        fields, _ = S.append_outcome(task_fields(), "One line.\n\nAnd another.",
                                     at="2026-09-05T00:00:00Z")
        self.assertNotIn("\n", fields["outcome"])
        # And the record renders, which is the thing newlines break.
        S.render_record(fields)

    def test_an_empty_outcome_is_refused(self):
        with self.assertRaises(S.StoreError):
            S.append_outcome(task_fields(), "   ")

    def test_text_carrying_the_separator_is_refused(self):
        """Otherwise one entry comes back out as two nobody wrote."""
        with self.assertRaises(S.StoreError):
            S.append_outcome(task_fields(), "shipped || did not ship")

    def test_entries_reads_nothing_as_no_entries(self):
        self.assertEqual(S.outcome_entries(None), [])
        self.assertEqual(S.outcome_entries(""), [])


class AppendOnly(unittest.TestCase):
    """The refusals. Same shape as runs/*.jsonl, same exception."""

    def test_replacement_raises(self):
        before = task_fields(outcome="2026-09-05: One.")
        after = task_fields(outcome="2026-09-06: Something else entirely.")
        with self.assertRaises(S.AppendOnlyViolation):
            S.assert_outcome_append_only(before, after)

    def test_deletion_raises(self):
        before = task_fields(outcome="2026-09-05: One.")
        with self.assertRaises(S.AppendOnlyViolation):
            S.assert_outcome_append_only(before, task_fields(outcome=None))

    def test_editing_the_last_entry_raises_even_though_it_is_a_prefix(self):
        """`shipped` growing into `shipped it` passes a naive prefix test and is
        still a rewrite of the last entry rather than a new one."""
        before = task_fields(outcome="2026-09-05: Shipped")
        after = task_fields(outcome="2026-09-05: Shipped it")
        with self.assertRaises(S.AppendOnlyViolation):
            S.assert_outcome_append_only(before, after)

    def test_a_real_append_passes(self):
        before = task_fields(outcome="2026-09-05: One.")
        after = task_fields(outcome="2026-09-05: One. || 2026-09-06: Two.")
        self.assertTrue(S.assert_outcome_append_only(before, after))

    def test_unchanged_passes(self):
        fields = task_fields(outcome="2026-09-05: One.")
        self.assertTrue(S.assert_outcome_append_only(fields, dict(fields)))


class WritingTaskRecords(unittest.TestCase):
    """The door. A write that replaces has to fail at the filesystem, not only
    in the helper -- the helper is easy to route around."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "P-0001-T03.md"
        self.addCleanup(self.dir.cleanup)

    def test_a_write_that_replaces_is_refused(self):
        S.write_task_record(self.path, task_fields(outcome="2026-09-05: One."))
        with self.assertRaises(S.AppendOnlyViolation):
            S.write_task_record(self.path, task_fields(outcome="2026-09-06: Other."))
        fields, _ = S.read_record(self.path)
        self.assertEqual(fields["outcome"], "2026-09-05: One.")

    def test_a_write_that_drops_the_field_is_refused(self):
        S.write_task_record(self.path, task_fields(outcome="2026-09-05: One."))
        with self.assertRaises(S.AppendOnlyViolation):
            S.write_task_record(self.path, task_fields())

    def test_a_write_that_appends_is_allowed_and_round_trips(self):
        S.write_task_record(self.path, task_fields(outcome="2026-09-05: One."))
        fields, body = S.read_record(self.path)
        fields, _ = S.append_outcome(fields, "Two.", at="2026-09-06T00:00:00Z")
        S.write_task_record(self.path, fields, body)
        fields, _ = S.read_record(self.path)
        self.assertEqual(len(S.outcome_entries(fields["outcome"])), 2)

    def test_it_re_reads_the_file_rather_than_trusting_memory(self):
        """PARKED.md item 17: two windows, no merge protection. A record written
        from a store loaded ten minutes ago must not silently lose what the
        other window appended in the meantime."""
        stale, _ = S.append_outcome(task_fields(), "One.", at="2026-09-05T00:00:00Z")
        S.write_task_record(self.path, stale)
        other_window, _ = S.append_outcome(stale, "Two.", at="2026-09-06T00:00:00Z")
        S.write_task_record(self.path, other_window)
        mine, _ = S.append_outcome(stale, "Three.", at="2026-09-06T00:00:00Z")
        with self.assertRaises(S.AppendOnlyViolation):
            S.write_task_record(self.path, mine)

    def test_a_state_transition_cannot_drop_an_outcome(self):
        """The transition path writes the whole record back, so it is the other
        way an outcome could be lost."""
        fields = task_fields(outcome="2026-09-05: One.", state="built")
        S.write_task_record(self.path, fields)
        moved, _, _ = S.transition_task(fields, "shipped")
        S.write_task_record(self.path, moved)
        fields, _ = S.read_record(self.path)
        self.assertEqual(fields["state"], "shipped")
        self.assertEqual(fields["outcome"], "2026-09-05: One.")


class NoGateOwesIt(unittest.TestCase):
    """The stage-gate question, answered in a test as well as in NOTES.md: no
    stage and no state owes an outcome. A gate that refused a transition for
    missing prose would be refusing on narrative."""

    def test_no_task_state_owes_outcome(self):
        for state, requirement in S.TASK_STATE_REQUIREMENTS.items():
            self.assertNotIn("outcome", requirement["fields"], state)

    def test_no_project_stage_owes_outcome(self):
        for stage, requirement in S.STAGE_REQUIREMENTS.items():
            self.assertNotIn("outcome", requirement["fields"], stage)

    def test_shipping_without_an_outcome_is_allowed(self):
        moved, notes, _ = S.transition_task(task_fields(state="built"), "shipped")
        self.assertEqual(moved["state"], "shipped")
        self.assertEqual([note for note in notes if "outcome" in note], [])


class Disclosive(unittest.TestCase):
    """Deliverable 1's second half, and the RI-08 shape an earlier session found three of:
    a disclosive field that ships one session ahead of its redaction."""

    def test_outcome_is_a_disclosive_field(self):
        self.assertIn("outcome", S.DISCLOSIVE_FIELDS)

    def test_the_redactor_replaces_it(self):
        payload = {"projects": {}, "tasks": {
            "P-0001-T03": task_fields(outcome="2026-09-05: Shipped for Acme Ltd.")}}
        presented = governance.present(payload)
        self.assertNotIn("Acme", json.dumps(presented))

    def test_the_audit_catches_it_on_the_serialised_payload(self):
        payload = {"projects": {}, "tasks": {
            "P-0001-T03": task_fields(outcome="2026-09-05: Shipped for Acme Ltd.")}}
        audit = governance.audit_presentation(payload, governance.present(payload))
        self.assertTrue(audit["clean"], audit["leaked"])
        self.assertGreaterEqual(audit["checked"], 1)

    def test_an_unredacted_outcome_fails_the_audit(self):
        """The audit has to be able to fail, or it is decoration."""
        payload = {"projects": {}, "tasks": {
            "P-0001-T03": task_fields(outcome="2026-09-05: Shipped for Acme Ltd.")}}
        leaky = {"projects": {}, "tasks": dict(payload["tasks"])}
        audit = governance.audit_presentation(payload, leaky)
        self.assertFalse(audit["clean"])

    def test_it_is_counted_in_the_export_manifest(self):
        payload = {"projects": {}, "tasks": {
            "P-0001-T03": task_fields(outcome="2026-09-05: Shipped.")}}
        self.assertEqual(S.disclosive_inventory(payload).get("outcome"), 1)


class PlanInTheRegister(unittest.TestCase):
    """Deliverable 2. `plan_row` is a list because the rows and the task records
    are not 1:1 -- P-0001-T10 was written for rows 7 AND 8."""

    def test_rows_read_back_as_ints(self):
        self.assertEqual(S.plan_rows_of({"plan_row": [7, 8]}), [7, 8])

    def test_a_scalar_on_disk_still_reads(self):
        self.assertEqual(S.plan_rows_of({"plan_row": "3"}), [3])

    def test_no_row_is_no_rows(self):
        self.assertEqual(S.plan_rows_of({}), [])
        self.assertEqual(S.plan_rows_of({"plan_row": None}), [])

    def test_the_view_names_every_row_and_the_tasks_that_claim_it(self):
        store_data = {"tasks": {
            "P-0001-T10": {"fields": task_fields(ref="P-0001-T10", plan_row=[7, 8],
                                                 depends_on=["P-0001-T08"])}}}
        view = S.plan_view(store_data)
        self.assertEqual([task["ref"] for task in view["rows"][7]], ["P-0001-T10"])
        self.assertEqual([task["ref"] for task in view["rows"][8]], ["P-0001-T10"])
        self.assertEqual(view["unclaimed"], [1, 2, 3, 4, 5, 6])

    def test_a_row_outside_the_plan_is_stray_not_silently_dropped(self):
        store_data = {"tasks": {
            "P-0001-T99": {"fields": task_fields(ref="P-0001-T99", plan_row=[99])}}}
        self.assertIn("99", S.plan_view(store_data)["stray"])

    def test_the_view_carries_the_outcome_entries(self):
        store_data = {"tasks": {"P-0001-T03": {"fields": task_fields(
            plan_row=[1], outcome="2026-09-05: One. || 2026-09-06: Two.")}}}
        self.assertEqual(len(S.plan_view(store_data)["rows"][1][0]["outcome"]), 2)


class TheCheckerSeesThePlan(unittest.TestCase):
    """`store.py --check` is what the store says about itself, and the plan is
    now something it can be wrong about."""

    def setUp(self):
        self.register = S.RefRegister(path=Path(tempfile.mkdtemp()) / "refs.json")

    def store_with(self, tasks):
        return {"projects": {"P-0001": {"fields": {"ref": "P-0001", "stage": "prototype"}}},
                "tasks": {ref: {"fields": fields} for ref, fields in tasks.items()},
                "runs": [], "decisions": [], "root": "."}

    def test_a_row_outside_the_plan_is_a_problem(self):
        store_data = self.store_with({"P-0001-T03": task_fields(plan_row=[99])})
        result = S.check(store_data, self.register)
        self.assertTrue(any("plan_row" in problem for problem in result["problems"]))

    def test_a_dangling_dependency_is_a_problem(self):
        store_data = self.store_with(
            {"P-0001-T03": task_fields(depends_on=["P-0001-T77"])})
        result = S.check(store_data, self.register)
        self.assertTrue(any("depends_on" in problem for problem in result["problems"]))

    def test_a_self_dependency_is_a_problem(self):
        store_data = self.store_with(
            {"P-0001-T03": task_fields(depends_on=["P-0001-T03"])})
        result = S.check(store_data, self.register)
        self.assertTrue(any("itself" in problem for problem in result["problems"]))

    def test_a_reversed_plan_order_is_a_problem(self):
        store_data = self.store_with({
            "P-0001-T03": task_fields(ref="P-0001-T03", plan_row=[1],
                                      depends_on=["P-0001-T04"]),
            "P-0001-T04": task_fields(ref="P-0001-T04", plan_row=[2]),
        })
        result = S.check(store_data, self.register)
        self.assertTrue(any("reversed" in problem for problem in result["problems"]))

    def test_the_real_ordering_is_not_a_problem(self):
        store_data = self.store_with({
            "P-0001-T03": task_fields(ref="P-0001-T03", plan_row=[1]),
            "P-0001-T04": task_fields(ref="P-0001-T04", plan_row=[2],
                                      depends_on=["P-0001-T03"]),
        })
        problems = S.check(store_data, self.register)["problems"]
        # The fixture register is empty, so the ref-register complaints are
        # expected noise here; what must be absent is a complaint about order.
        self.assertEqual([problem for problem in problems
                          if "reversed" in problem or "depends_on" in problem
                          or "plan_row" in problem], [])


if __name__ == "__main__":
    unittest.main()
