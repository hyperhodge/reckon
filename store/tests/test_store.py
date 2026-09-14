#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s store/tests

Plain unittest, no third-party dependencies. Every fixture is written to a
temporary directory; nothing here reads or writes ~/.claude, the archive, the
real store, or the network.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import store  # noqa: E402


SESSION_A = "aaaaaaaa-1111-2222-3333-444444444444"
SESSION_B = "bbbbbbbb-1111-2222-3333-444444444444"


def ledger_run(session, ref, cost=1.0, started="2026-09-01T10:00:00.000Z", **extra):
    record = {
        "ref": ref, "claude_session_id": session, "cost_usd": cost,
        "started_at": started, "ended_at": "2026-09-01T11:00:00.000Z",
        "model": "claude-test-1", "surface": "code", "rail": "subscription",
        "task_ref": None, "status": None, "stop_reason": "claude_complete",
        "exit_code": None, "usage": {"output_tokens": 10}, "outputs": [],
        "commits": [], "transcript_path": "/tmp/nowhere.jsonl",
    }
    record.update(extra)
    return record


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "projects").mkdir()
        (self.root / "tasks").mkdir()
        (self.root / "runs").mkdir()
        (self.root / "store").mkdir()
        (self.root / "ledger").mkdir()
        self.refs = self.root / "store" / "refs.json"

    def tearDown(self):
        self.dir.cleanup()

    def register(self):
        return store.RefRegister.load(self.refs)


# ---------------------------------------------------------------- ref allocation

class TestRefAllocation(Temp):

    def test_projects_are_sequential_and_zero_padded(self):
        reg = self.register()
        self.assertEqual(reg.allocate_project("alpha"), "P-0001")
        self.assertEqual(reg.allocate_project("beta"), "P-0002")

    def test_allocation_is_idempotent_on_the_natural_key(self):
        """Asking twice for the same thing must not burn a ref."""
        reg = self.register()
        first = reg.allocate_project("alpha")
        self.assertEqual(reg.allocate_project("alpha"), first)
        self.assertEqual(reg.allocate_project("beta"), "P-0002")

    def test_a_ref_survives_a_reload(self):
        """The whole reason a store can mint a sequential ref and the Recorder
        cannot: allocation is a durable write, not a recomputation."""
        reg = self.register()
        reg.allocate_project("alpha")
        reg.allocate_run(SESSION_A)
        reg.save()
        again = store.RefRegister.load(self.refs)
        self.assertEqual(again.allocate_project("alpha"), "P-0001")
        self.assertEqual(again.allocate_run(SESSION_A), "R-0001")

    def test_an_older_arrival_gets_the_next_ref_and_never_renumbers(self):
        """A transcript from July arriving after one from September takes the
        next number. This is the failure the Recorder's derived refs avoid by
        not being sequential at all, and the store avoids by writing them down."""
        reg = self.register()
        september = reg.allocate_run(SESSION_A)
        july = reg.allocate_run(SESSION_B)
        self.assertEqual(september, "R-0001")
        self.assertEqual(july, "R-0002")
        self.assertEqual(reg.allocate_run(SESSION_A), "R-0001")

    def test_tasks_number_within_their_project(self):
        reg = self.register()
        reg.adopt("P-0001", "one", "project")
        reg.adopt("P-0002", "two", "project")
        self.assertEqual(reg.allocate_task("P-0001", "a"), "P-0001-T01")
        self.assertEqual(reg.allocate_task("P-0002", "b"), "P-0002-T01")
        self.assertEqual(reg.allocate_task("P-0001", "c"), "P-0001-T02")

    def test_a_hole_in_the_sequence_is_kept_not_backfilled(self):
        """P-0001 really has no T02: the TASK-NNN files are numbered globally
        and the refs per project, so TASK-002 took a P-0007 ref. The allocator
        continues from the highest and never fills the hole."""
        reg = self.register()
        reg.adopt("P-0001", "p", "project")
        reg.adopt("P-0001-T01", "one", "task")
        reg.adopt("P-0001-T03", "three", "task")
        self.assertEqual(reg.allocate_task("P-0001", "next"), "P-0001-T04")

    def test_runs_key_on_the_session_id(self):
        """§2.3: claude_session_id is the only identifier the transcripts carry."""
        reg = self.register()
        ref = reg.allocate_run(SESSION_A)
        self.assertEqual(reg.natural_key(ref), SESSION_A)


class TestRefCollision(Temp):

    def test_rebinding_a_ref_to_a_different_key_raises(self):
        reg = self.register()
        reg.adopt("P-0001", "alpha", "project")
        with self.assertRaises(store.RefCollision):
            reg.adopt("P-0001", "beta", "project")

    def test_adopting_the_same_ref_for_the_same_key_is_fine(self):
        reg = self.register()
        reg.adopt("P-0001", "alpha", "project")
        self.assertEqual(reg.adopt("P-0001", "alpha", "project"), "P-0001")

    def test_a_ref_is_never_reused_after_the_record_is_gone(self):
        reg = self.register()
        reg.allocate_project("alpha")
        # the record is deleted from disk; the binding is not
        self.assertEqual(reg.allocate_project("beta"), "P-0002")

    def test_allocating_over_an_alias_raises(self):
        reg = self.register()
        ref = reg.allocate_run(SESSION_A)
        reg.add_alias("R-aaaaaaaa", ref)
        with self.assertRaises(store.RefCollision):
            reg.adopt("R-aaaaaaaa", SESSION_B, "run")

    def test_an_alias_cannot_shadow_an_allocated_ref(self):
        reg = self.register()
        first = reg.allocate_run(SESSION_A)
        second = reg.allocate_run(SESSION_B)
        with self.assertRaises(store.RefCollision):
            reg.add_alias(first, second)

    def test_an_alias_cannot_be_repointed(self):
        reg = self.register()
        first = reg.allocate_run(SESSION_A)
        second = reg.allocate_run(SESSION_B)
        reg.add_alias("R-aaaaaaaa", first)
        reg.add_alias("R-aaaaaaaa", first)          # idempotent
        with self.assertRaises(store.RefCollision):
            reg.add_alias("R-aaaaaaaa", second)

    def test_an_alias_must_point_at_something_allocated(self):
        reg = self.register()
        with self.assertRaises(store.StoreError):
            reg.add_alias("R-aaaaaaaa", "R-9999")


class TestProvisionalRunAlias(Temp):
    """ledger/run-tasks.json and wrapper/gate.json's prior basis both cite the
    Recorder's R-xxxxxxxx refs. They must resolve, not be rewritten."""

    def test_ingest_keeps_the_provisional_ref_as_an_alias(self):
        reg = self.register()
        records, _ = store.ingest_runs([ledger_run(SESSION_A, "R-aaaaaaaa")], reg, [])
        self.assertEqual(records[0]["ref"], "R-0001")
        self.assertEqual(records[0]["aliases"], ["R-aaaaaaaa"])

    def test_the_provisional_ref_resolves_to_the_canonical_one(self):
        reg = self.register()
        store.ingest_runs([ledger_run(SESSION_A, "R-aaaaaaaa")], reg, [])
        self.assertEqual(reg.resolve("R-aaaaaaaa"), "R-0001")
        self.assertEqual(reg.resolve("R-0001"), "R-0001")

    def test_an_unknown_ref_resolves_to_itself_rather_than_raising(self):
        """A citation in an old file must still read even if nothing knows it."""
        self.assertEqual(self.register().resolve("R-deadbeef"), "R-deadbeef")

    def test_re_ingesting_does_not_mint_a_second_ref(self):
        reg = self.register()
        first, _ = store.ingest_runs([ledger_run(SESSION_A, "R-aaaaaaaa")], reg, [])
        second, report = store.ingest_runs([ledger_run(SESSION_A, "R-aaaaaaaa")],
                                           reg, first)
        self.assertEqual(second, [])
        self.assertEqual(report["unchanged"], ["R-0001"])


# ---------------------------------------------------------------- front matter

class TestFrontMatterRoundTrip(Temp):

    def test_scalars_lists_and_nulls_survive(self):
        fields = {"ref": "P-0001", "name": "thing", "depends_on": ["P-0002", "P-0003"],
                  "parked_until": None, "preferences_inherited": True,
                  "budget_tokens_default": 250000}
        text = store.render_record(fields, "# body\n\nprose\n")
        back, body = store.parse_record(text)
        self.assertEqual(back["depends_on"], ["P-0002", "P-0003"])
        self.assertIsNone(back["parked_until"])
        self.assertIs(back["preferences_inherited"], True)
        self.assertEqual(back["ref"], "P-0001")
        self.assertIn("prose", body)

    def test_an_empty_list_round_trips_as_a_list_not_a_none(self):
        back, _ = store.parse_record(store.render_record({"runs": []}))
        self.assertEqual(back["runs"], [])

    def test_yes_and_no_stay_strings(self):
        """§2.1's is_ai_system is yes | no | contested. Coercing two thirds of a
        three-valued field to bool loses the third."""
        back, _ = store.parse_record(store.render_record({"is_ai_system": "contested"}))
        self.assertEqual(back["is_ai_system"], "contested")
        back, _ = store.parse_record(store.render_record({"is_ai_system": "yes"}))
        self.assertEqual(back["is_ai_system"], "yes")

    def test_the_launcher_can_read_what_the_store_writes(self):
        """The reason parse_record delegates: store files must stay readable by
        wrapper/launch.py, and using its parser is the only way to guarantee it."""
        sys.path.insert(0, str(ROOT / "wrapper"))
        import launch
        text = store.render_record(
            {"ref": "P-0001-T09", "state": "approved", "est_p95_usd": "8.00",
             "runs": ["R-0001", "R-0002"]}, "# brief\n")
        front, _ = launch.parse_front_matter(text)
        self.assertEqual(front["est_p95_usd"], "8.00")
        self.assertEqual(front["runs"], "[R-0001, R-0002]")   # opaque, and fine

    def test_a_newline_in_a_field_is_refused_rather_than_mangled(self):
        with self.assertRaises(store.StoreError):
            store.render_record({"brief": "one\ntwo"})

    def test_write_record_refuses_what_it_cannot_read_back(self):
        path = self.root / "projects" / "P-0001.md"
        store.write_record(path, {"ref": "P-0001", "name": "x"}, "body")
        back, _ = store.read_record(path)
        self.assertEqual(back["ref"], "P-0001")


# ---------------------------------------------------------------- stage gates

class TestAdvisoryStageGates(Temp):

    def test_capture_owes_two_fields_and_nothing_else(self):
        """§3.1: capture costs two fields and eight seconds."""
        debts = store.stage_debt({"stage": "captured", "name": "x", "one_liner": "y"})
        self.assertEqual(debts, [])

    def test_a_later_stage_owes_the_earlier_stages_too(self):
        debts = store.stage_debt({"stage": "scoped", "name": "x"})
        owed = {debt["field"] for debt in debts}
        self.assertIn("one_liner", owed)      # from captured
        self.assertIn("context", owed)        # from screened
        self.assertIn("act_tier", owed)       # from scoped

    def test_a_debt_names_the_stage_and_the_exit_test_it_comes_from(self):
        debt = store.stage_debt({"stage": "production", "name": "x"})[-1]
        self.assertEqual(debt["field"], "next_review")
        self.assertEqual(debt["owed_from"], "production")
        self.assertIn("Reviews on cadence", debt["exit_test"])

    def test_a_transition_reports_debts_and_now_refuses(self):
        """An earlier session's advisory test, inverted on purpose in an earlier session.

        The old version asserted the transition moved and reported. Enforcement
        was always this row's job (PLAN.md row 6), so this is the same kind of
        deliberate replacement as an earlier session's priors test: the assertion did not
        rot, the behaviour changed underneath it and the change is the point."""
        fields = {"ref": "P-0009", "stage": "captured", "name": "x"}
        with self.assertRaises(store.StageGateRefused) as caught:
            store.transition_project(fields, "validated")
        self.assertEqual(fields["stage"], "captured", "the caller's dict is untouched")
        self.assertTrue(caught.exception.debts)
        self.assertEqual(caught.exception.to_stage, "validated")

    def test_the_refusal_names_every_debt_and_its_exit_test(self):
        """§3.1: a gate that says no without saying what it wants is the flat
        thirty-field form §3.1 rejects, wearing a different hat."""
        with self.assertRaises(store.StageGateRefused) as caught:
            store.transition_project({"ref": "P-0009", "stage": "captured",
                                      "name": "x"}, "production")
        message = str(caught.exception)
        for debt in caught.exception.debts:
            self.assertIn(debt["field"], message)
            self.assertIn(debt["exit_test"], message)
        self.assertIn("act_tier", message)
        self.assertIn("Reviews on cadence", message)

    def test_enforcement_can_be_previewed_but_the_record_says_so(self):
        _, transition, debts = store.transition_project(
            {"ref": "P-0009", "stage": "captured"}, "production", enforce=False)
        self.assertGreater(len(debts), 5)
        self.assertFalse(transition["enforced"])
        self.assertIn("advisory", transition["basis"])

    def test_a_clean_transition_is_allowed_and_records_nothing_owed(self):
        fields = {"ref": "P-0009", "stage": "screened", "name": "x", "one_liner": "y",
                  "context": "business", "type": "code", "value_basis": "capability",
                  "is_ai_system": "no", "my_role": "n/a", "act_tier": "minimal",
                  "directory": "~/x"}
        updated, transition, debts = store.transition_project(fields, "scoped")
        self.assertEqual(updated["stage"], "scoped")
        self.assertEqual(debts, [])
        self.assertTrue(transition["enforced"])
        self.assertFalse(transition["overridden"])

    def test_an_override_needs_a_name_and_a_reason(self):
        """§3.1's gates are governance; an unrecorded workaround is not evidence."""
        for override in ({"by": "ada"}, {"reason": "shipping anyway"}, {"by": " "}):
            with self.assertRaises(store.OverrideNotRecorded):
                store.transition_project({"ref": "P-0009", "stage": "captured"},
                                         "production", override=override)

    def test_a_recorded_override_passes_the_gate_and_is_on_the_record(self):
        updated, transition, debts = store.transition_project(
            {"ref": "P-0009", "stage": "captured"}, "production",
            override={"by": "ada", "reason": "demo tomorrow; debts tracked"})
        self.assertEqual(updated["stage"], "production")
        self.assertTrue(transition["overridden"])
        self.assertEqual(transition["override"]["by"], "ada")
        self.assertIn("demo tomorrow", transition["override"]["reason"])
        self.assertTrue(transition["override"]["at"])
        self.assertTrue(transition["debts"], "the debts survive the override")

    def test_a_clean_transition_is_not_marked_overridden_even_if_one_is_offered(self):
        fields = {"ref": "P-0009", "stage": "prototype", "name": "x", "one_liner": "y",
                  "context": "business", "type": "code", "value_basis": "capability",
                  "is_ai_system": "no", "my_role": "n/a", "act_tier": "minimal",
                  "directory": "~/x", "output_location": "~/x",
                  "value_estimate": "1h", "third_party_output": "none"}
        _, transition, _ = store.transition_project(
            fields, "validated", override={"by": "ada", "reason": "belt and braces"})
        self.assertFalse(transition["overridden"])
        self.assertIsNone(transition["override"])

    def test_a_retreat_is_reported_and_allowed(self):
        """A retreat reduces what the record claims. Refusing it would trap a
        project in a stage it has outgrown downwards."""
        updated, transition, debts = store.transition_project(
            {"ref": "P-0009", "stage": "production", "name": "x"}, "screened")
        self.assertEqual(updated["stage"], "screened")
        self.assertEqual(transition["direction"], "retreat")
        self.assertTrue(debts)
        self.assertFalse(transition["overridden"])

    def test_retirement_is_an_exit_and_is_never_gated(self):
        """§3.1's exit test for Retired is 'Terminal. Record and learnings
        retained'. Refusing it would hold the project in Production."""
        updated, transition, debts = store.transition_project(
            {"ref": "P-0009", "stage": "production", "name": "x"}, "retired")
        self.assertEqual(updated["stage"], "retired")
        self.assertEqual(transition["direction"], "retire")
        self.assertTrue(debts)
        self.assertIn("Terminal", transition["basis"])

    def test_enforcement_gates_transitions_and_never_existence(self):
        """§3.1's principle, unchanged: fields become mandatory as a project
        advances, never at creation. A record already sitting in a stage it owes
        fields for must stay readable."""
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "production", "name": "x"}, "")
        loaded = store.load_store(self.root)
        self.assertIn("P-0001", loaded["projects"])
        self.assertTrue(store.stage_debt(loaded["projects"]["P-0001"]["fields"]))

    def test_an_unknown_stage_is_refused(self):
        """Advisory about debts is not advisory about the vocabulary."""
        with self.assertRaises(store.StoreError):
            store.transition_project({"ref": "P-0009", "stage": "captured"}, "launched")

    def test_check_reports_a_debt_as_an_advisory_not_a_problem(self):
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "production", "name": "x"}, "")
        reg = self.register()
        reg.adopt("P-0001", "x", "project")
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("next_review" in line for line in result["advisories"]))


# ---------------------------------------------------------------- task states

class TestTaskStates(Temp):

    def test_a_task_in_p0000_cannot_leave_drafted(self):
        """§3.2, and the one hard rule among the gates this session."""
        task = {"ref": "P-0000-T01", "project_ref": "P-0000", "state": "drafted"}
        with self.assertRaises(store.StoreError) as caught:
            store.transition_task(task, "approved")
        self.assertIn("P-0000", str(caught.exception))

    def test_the_same_task_may_stay_drafted(self):
        task = {"ref": "P-0000-T01", "project_ref": "P-0000", "state": "drafted"}
        updated, _, _ = store.transition_task(task, "drafted")
        self.assertEqual(updated["state"], "drafted")

    def test_the_same_move_is_allowed_once_the_project_is_sorted(self):
        task = {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "drafted",
                "approved_by": "ada", "approved_at": "2026-09-06T08:15:19Z"}
        updated, notes, transition = store.transition_task(task, "approved")
        self.assertEqual(updated["state"], "approved")
        self.assertEqual(notes, [])
        self.assertTrue(transition["enforced"])

    def test_p0000_blocks_the_off_ramp_too(self):
        """`abandoned` is still leaving `drafted`."""
        with self.assertRaises(store.StoreError):
            store.transition_task({"ref": "P-0000-T01", "project_ref": "P-0000",
                                   "state": "drafted"}, "abandoned")

    def test_a_skipped_state_is_reported_and_allowed(self):
        task = {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "drafted",
                "approved_by": "ada"}
        updated, notes, _ = store.transition_task(task, "shipped")
        self.assertEqual(updated["state"], "shipped")
        self.assertTrue(any("skipped" in note for note in notes))

    def test_approval_without_a_name_is_now_refused(self):
        """§3.2: approval is the cowork-to-code boundary and is recorded with a
        name and a timestamp, because that separation is the control a client
        recognises. An earlier session reported it in a note; a control that reports and
        lets the work through is not that control."""
        with self.assertRaises(store.StageGateRefused) as caught:
            store.transition_task(
                {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "drafted"},
                "approved")
        self.assertIn("approved_by", str(caught.exception))
        self.assertIn("approved_at", str(caught.exception))
        self.assertIn("cowork-to-code", str(caught.exception))

    def test_approval_without_a_timestamp_is_refused_too(self):
        with self.assertRaises(store.StageGateRefused):
            store.transition_task(
                {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "drafted",
                 "approved_by": "ada"}, "approved")

    def test_an_approval_gate_can_be_overridden_on_the_record(self):
        _, _, transition = store.transition_task(
            {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "drafted"},
            "approved", override={"by": "ada", "reason": "verbal, minuted"})
        self.assertTrue(transition["overridden"])
        self.assertEqual(transition["override"]["by"], "ada")

    def test_the_p0000_rule_cannot_be_overridden(self):
        """It is a rule about where the work belongs, not a field the record
        owes, so it is not a debt and an override does not reach it."""
        with self.assertRaises(store.StoreError) as caught:
            store.transition_task(
                {"ref": "P-0000-T01", "project_ref": "P-0000", "state": "drafted"},
                "approved", override={"by": "ada", "reason": "in a hurry"})
        self.assertNotIsInstance(caught.exception, store.StageGateRefused)
        self.assertIn("not overridable", str(caught.exception))

    def test_a_state_already_held_is_not_re_gated(self):
        """Enforcement is about entering a state, not about sitting in one."""
        updated, _, transition = store.transition_task(
            {"ref": "P-0001-T01", "project_ref": "P-0001", "state": "approved"},
            "approved")
        self.assertEqual(updated["state"], "approved")
        self.assertTrue(transition["debts"], "still owed, and still reported")
        self.assertFalse(transition["overridden"])

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.transition_task({"ref": "P-0001-T01", "project_ref": "P-0001",
                                   "state": "drafted"}, "done")

    def test_check_flags_a_moved_task_still_sitting_in_p0000(self):
        store.write_record(self.root / "projects" / "P-0000.md",
                           {"ref": "P-0000", "stage": "captured", "name": "inbox",
                            "one_liner": "x"}, "")
        store.write_record(self.root / "tasks" / "P-0000-T01.md",
                           {"ref": "P-0000-T01", "project_ref": "P-0000",
                            "state": "running"}, "")
        reg = self.register()
        reg.adopt("P-0000", "inbox", "project")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("P-0000" in line and "§3.2" in line
                            for line in result["problems"]))


# ---------------------------------------------------------------- append-only

class TestAppendOnly(Temp):

    def path(self):
        return self.root / "runs" / "2026-09.jsonl"

    def test_appending_keeps_the_earlier_lines_byte_for_byte(self):
        path = self.path()
        store.append_runs(path, [{"ref": "R-0001", "cost_usd": 1.0}])
        before = path.read_text()
        store.append_runs(path, [{"ref": "R-0002", "cost_usd": 2.0}])
        self.assertTrue(path.read_text().startswith(before))

    def test_a_changed_line_is_a_violation(self):
        before = [{"ref": "R-0001", "cost_usd": 1.0}]
        after = [{"ref": "R-0001", "cost_usd": 9.0}, {"ref": "R-0002"}]
        with self.assertRaises(store.AppendOnlyViolation) as caught:
            store.assert_append_only(before, after)
        self.assertIn("line 1", str(caught.exception))

    def test_a_removed_line_is_a_violation(self):
        with self.assertRaises(store.AppendOnlyViolation):
            store.assert_append_only([{"ref": "R-0001"}, {"ref": "R-0002"}],
                                     [{"ref": "R-0001"}])

    def test_append_refuses_when_the_file_moved_under_it(self):
        path = self.path()
        store.append_runs(path, [{"ref": "R-0001"}])
        stale = []                                    # what we thought was there
        with self.assertRaises(store.AppendOnlyViolation):
            store.append_runs(path, [{"ref": "R-0002"}], existing=[{"ref": "R-9999"}])
        self.assertEqual(len(store.read_runs_file(path)), 1)
        del stale

    def test_a_correction_supersedes_rather_than_edits(self):
        """§8.5. An earlier session's own run was read at $4.75 and settled at $5.81 four
        minutes later once the archive LaunchAgent picked up its tail. Both
        figures survive; the later one is the current view."""
        reg = self.register()
        first, _ = store.ingest_runs([ledger_run(SESSION_A, "R-aaaaaaaa", cost=4.75)],
                                     reg, [])
        second, report = store.ingest_runs(
            [ledger_run(SESSION_A, "R-aaaaaaaa", cost=5.81)], reg, first)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["supersedes"]["previous_cost_usd"], 4.75)
        self.assertIn("cost_usd", second[0]["supersedes"]["changed"])
        self.assertEqual(report["superseded"][0]["to_cost_usd"], 5.81)

    def test_the_current_view_is_the_last_record_per_ref(self):
        records = [{"ref": "R-0001", "cost_usd": 4.75}, {"ref": "R-0002", "cost_usd": 1.0},
                   {"ref": "R-0001", "cost_usd": 5.81}]
        view = store.current_runs(records)
        self.assertEqual(view["R-0001"]["cost_usd"], 5.81)
        self.assertEqual(len(view), 2)

    def test_both_records_are_still_on_disk_after_a_correction(self):
        path = self.path()
        store.append_runs(path, [{"ref": "R-0001", "cost_usd": 4.75, "started_at": "x"}])
        store.append_runs(path, [{"ref": "R-0001", "cost_usd": 5.81, "started_at": "x"}])
        self.assertEqual(len(store.read_runs_file(path)), 2)
        self.assertEqual(len(store.current_runs(store.read_runs_file(path))), 1)

    def test_check_counts_superseded_records_out_loud(self):
        store.append_runs(self.path(), [
            {"ref": "R-0001", "claude_session_id": SESSION_A, "cost_usd": 4.75},
            {"ref": "R-0001", "claude_session_id": SESSION_A, "cost_usd": 5.81}])
        reg = self.register()
        reg.adopt("R-0001", SESSION_A, "run")
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["counts"], {"projects": 0, "tasks": 0,
                                            "runs": 1, "run_records": 2})
        self.assertTrue(any("superseded" in line for line in result["advisories"]))


# ---------------------------------------------------------------- runs and cost

class TestRunsAreCanonicalHere(Temp):

    def test_cost_is_copied_from_the_ledger_never_recomputed(self):
        """A third arithmetic would be a third number to reconcile. recorder.py
        already agrees with aggregate.py to $0.000002."""
        reg = self.register()
        records, _ = store.ingest_runs(
            [ledger_run(SESSION_A, "R-aaaaaaaa", cost=13.930001)], reg, [])
        self.assertEqual(records[0]["cost_usd"], 13.930001)
        self.assertIn("ledger/runs.json", records[0]["recorded_from"])

    def test_a_run_lands_in_the_month_it_started(self):
        self.assertEqual(store.runs_path("2026-07-29T09:53:00Z", self.root / "runs").name,
                         "2026-07.jsonl")

    def test_no_pricing_arithmetic_lives_in_this_module(self):
        """Property of the source file, not of one code path. The same shape as
        recorder.py's no-mtime test: pricing belongs to aggregate.py."""
        source = (ROOT / "store" / "store.py").read_text()
        for banned in ("prices.json", "cache_read_input_tokens *", "/ 1_000_000",
                       "price_for", "message_cost"):
            self.assertNotIn(banned, source, f"{banned!r} suggests re-priced cost")

    def test_no_transcript_reading_lives_in_this_module(self):
        """The store writes runs/*.jsonl, so a .jsonl mention is expected. What
        must not appear is the archive, the live tree, or a timestamp scan --
        transcripts belong to ledger/recorder.py and this ingests its output."""
        source = (ROOT / "store" / "store.py").read_text()
        for banned in ("~/.claude", ".claude/projects", "claude-ledger",
                       "st_mtime", "mtime", "Application Support"):
            self.assertNotIn(banned, source,
                             f"{banned!r} suggests the store reads transcripts")


# ---------------------------------------------------------------- whole store

class TestCheckAndExport(Temp):

    def seed(self):
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "prototype", "name": "reg",
                            "one_liner": "x", "context": "business", "type": "code",
                            "value_basis": "capability", "is_ai_system": "yes",
                            "my_role": "deployer", "act_tier": "minimal",
                            "directory": "~/x", "output_location": "~/x",
                            "value_estimate": "1h"}, "")
        store.write_record(self.root / "tasks" / "P-0001-T01.md",
                           {"ref": "P-0001-T01", "project_ref": "P-0001",
                            "state": "approved", "approved_by": "ada",
                            "runs": ["R-0001"]}, "")
        reg = self.register()
        reg.adopt("P-0001", "reg", "project")
        reg.adopt("P-0001-T01", "t", "task")
        records, _ = store.ingest_runs(
            [ledger_run(SESSION_A, "R-aaaaaaaa", task_ref="P-0001-T01")], reg, [])
        store.append_runs(self.root / "runs" / "2026-09.jsonl", records)
        reg.save()
        return reg

    def test_a_clean_store_reports_no_problems(self):
        reg = self.seed()
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["counts"]["projects"], 1)

    def test_an_orphan_task_is_a_problem(self):
        reg = self.seed()
        store.write_record(self.root / "tasks" / "P-0099-T01.md",
                           {"ref": "P-0099-T01", "project_ref": "P-0099",
                            "state": "drafted"}, "")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("P-0099" in line for line in result["problems"]))

    def test_a_project_missing_from_the_register_is_a_problem(self):
        reg = self.seed()
        store.write_record(self.root / "projects" / "P-0042.md",
                           {"ref": "P-0042", "stage": "captured", "name": "n",
                            "one_liner": "o"}, "")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("refs.json" in line for line in result["problems"]))

    def test_a_parked_project_without_a_reason_is_a_problem(self):
        """§3.1: Parked keeps its reason and its revisit date, always."""
        reg = self.seed()
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "prototype", "name": "reg",
                            "one_liner": "x", "parked_until": "2026-12-01"}, "")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("parked" in line for line in result["problems"]))

    def test_export_carries_the_superseded_records_too(self):
        """An export showing only the current view would quietly disagree with
        the append-only log it came from."""
        reg = self.seed()
        path = self.root / "runs" / "2026-09.jsonl"
        store.append_runs(path, [dict(store.read_runs_file(path)[0], cost_usd=9.0)])
        payload = store.export(store.load_store(self.root), reg)
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(len(payload["superseded_runs"]), 1)
        self.assertEqual(payload["runs"][0]["cost_usd"], 9.0)

    def test_export_names_the_disclosive_fields_for_presentation_mode(self):
        """§8.5: paths can be disclosive even when contents are not. An earlier session
        owns presentation mode; this is the list it needs, so that the store is
        not designed in a way that makes it impossible."""
        payload = store.export(store.load_store(self.root), self.seed())
        self.assertIn("directory", payload["disclosive_fields"])
        self.assertIn("output_location", payload["disclosive_fields"])

    def test_export_says_the_cost_is_a_shadow_price(self):
        payload = store.export(store.load_store(self.root), self.seed())
        self.assertIn("not a bill", payload["basis"])

    def test_export_is_json_serialisable_whole(self):
        """§8.5: full JSON export of the whole store on demand, no pagination."""
        payload = store.export(store.load_store(self.root), self.seed())
        self.assertIsInstance(json.loads(json.dumps(payload)), dict)


class TestCli(Temp):

    def run_cli(self, argv):
        import io
        buffer = io.StringIO()
        code = store.main(argv + ["--root", str(self.root)], out=buffer)
        return code, buffer.getvalue()

    def test_check_on_an_empty_store_is_clean(self):
        code, output = self.run_cli(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("no problems", output)

    def test_ingest_dry_run_writes_nothing(self):
        (self.root / "ledger" / "runs.json").write_text(json.dumps(
            {"runs": [ledger_run(SESSION_A, "R-aaaaaaaa")]}))
        code, output = self.run_cli(["--ingest-runs", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was written", output)
        self.assertEqual(list((self.root / "runs").glob("*.jsonl")), [])
        self.assertFalse(self.refs.exists())

    def test_ingest_writes_the_run_and_the_register(self):
        (self.root / "ledger" / "runs.json").write_text(json.dumps(
            {"runs": [ledger_run(SESSION_A, "R-aaaaaaaa")]}))
        code, output = self.run_cli(["--ingest-runs"])
        self.assertEqual(code, 0)
        self.assertIn("new        R-0001", output)
        self.assertTrue(self.refs.exists())
        self.assertEqual(len(store.read_runs_file(self.root / "runs" / "2026-09.jsonl")), 1)

    def test_show_accepts_a_provisional_ref(self):
        (self.root / "ledger" / "runs.json").write_text(json.dumps(
            {"runs": [ledger_run(SESSION_A, "R-aaaaaaaa")]}))
        self.run_cli(["--ingest-runs"])
        code, output = self.run_cli(["--show", "R-aaaaaaaa"])
        self.assertEqual(code, 0)
        self.assertIn('"ref": "R-0001"', output)

    def test_show_on_an_unknown_ref_exits_non_zero(self):
        code, output = self.run_cli(["--show", "P-9999"])
        self.assertEqual(code, 1)
        self.assertIn("not in the store", output)


if __name__ == "__main__":
    unittest.main()


class TestTheLinkCheckedFromBothEnds(Temp):
    """Task.runs and the Run's task_ref are the same fact written twice, because
    ledger/run-tasks.json carries evidence prose the store does not. The one
    thing that must not happen is the two drifting apart quietly."""

    def seed(self, task_ref_on_run="P-0001-T01", runs_on_task=("R-0001",)):
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "captured", "name": "reg",
                            "one_liner": "x"}, "")
        store.write_record(self.root / "tasks" / "P-0001-T01.md",
                           {"ref": "P-0001-T01", "project_ref": "P-0001",
                            "state": "shipped", "runs": list(runs_on_task)}, "")
        store.write_record(self.root / "tasks" / "P-0001-T02.md",
                           {"ref": "P-0001-T02", "project_ref": "P-0001",
                            "state": "shipped", "runs": []}, "")
        reg = self.register()
        reg.adopt("P-0001", "reg", "project")
        records, _ = store.ingest_runs(
            [ledger_run(SESSION_A, "R-aaaaaaaa", task_ref=task_ref_on_run)], reg, [])
        store.append_runs(self.root / "runs" / "2026-09.jsonl", records)
        return reg

    def test_an_agreeing_link_is_clean(self):
        result = store.check(store.load_store(self.root), self.seed())
        self.assertEqual(result["problems"], [])

    def test_a_disagreeing_link_is_a_problem(self):
        reg = self.seed(task_ref_on_run="P-0001-T02", runs_on_task=("R-0001",))
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("disagrees with itself" in line
                            for line in result["problems"]))

    def test_a_task_citing_a_run_that_is_not_here_is_a_problem(self):
        reg = self.seed(runs_on_task=("R-0001", "R-0404"))
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("R-0404" in line for line in result["problems"]))

    def test_two_tasks_cannot_claim_one_run(self):
        """§2: cost rolls up one path with no ambiguity."""
        reg = self.seed()
        store.write_record(self.root / "tasks" / "P-0001-T02.md",
                           {"ref": "P-0001-T02", "project_ref": "P-0001",
                            "state": "shipped", "runs": ["R-0001"]}, "")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("at most one Task" in line
                            for line in result["problems"]))

    def test_a_task_citing_a_run_by_its_provisional_ref_still_resolves(self):
        reg = self.seed(runs_on_task=("R-aaaaaaaa",))
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])

    def test_a_run_linked_but_not_listed_is_an_advisory_not_a_problem(self):
        reg = self.seed(runs_on_task=())
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("not in its `runs` list" in line
                            for line in result["advisories"]))


class TestHandoffRefsAreRegistered(Temp):
    """§4's handoff files mint a ref in their own front matter. On 6 September
    TASK-007-risk-model.md held P-0001-T07 without registering it, and the
    allocator handed P-0001-T07 to a later task. A ref is only unique if every
    ref is visible to the register."""

    def handoff(self, name, ref):
        store.write_record(self.root / name, {"ref": ref, "state": "approved"}, "# brief\n")

    def test_an_unregistered_handoff_ref_is_a_problem(self):
        self.handoff("TASK-009-thing.md", "P-0001-T09")
        result = store.check(store.load_store(self.root), self.register())
        self.assertTrue(any("not in store/refs.json" in line
                            for line in result["problems"]))

    def test_a_registered_handoff_ref_is_clean(self):
        self.handoff("TASK-009-thing.md", "P-0001-T09")
        store.write_record(self.root / "tasks" / "P-0001-T09.md",
                           {"ref": "P-0001-T09", "project_ref": "P-0001",
                            "state": "approved", "approved_by": "ada"}, "")
        store.write_record(self.root / "projects" / "P-0001.md",
                           {"ref": "P-0001", "stage": "captured", "name": "x",
                            "one_liner": "y"}, "")
        reg = self.register()
        reg.adopt("P-0001", "x", "project")
        reg.adopt("P-0001-T09", "TASK-009-thing.md", "task")
        self.assertEqual(store.check(store.load_store(self.root), reg)["problems"], [])

    def test_a_ref_registered_to_a_different_file_is_a_problem(self):
        """The exact 6 September failure: two files, one ref."""
        self.handoff("TASK-009-thing.md", "P-0001-T09")
        reg = self.register()
        reg.adopt("P-0001-T09", "TASK-010-other.md", "task")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("registered to TASK-010-other.md" in line
                            for line in result["problems"]))

    def test_a_handoff_with_no_ref_is_a_problem(self):
        store.write_record(self.root / "TASK-009-thing.md", {"state": "approved"}, "")
        result = store.check(store.load_store(self.root), self.register())
        self.assertTrue(any("no ref" in line for line in result["problems"]))

    def test_a_registered_handoff_with_no_task_record_is_an_advisory(self):
        """Earlier sessions shipped without one. Worth saying, not worth refusing."""
        self.handoff("TASK-009-thing.md", "P-0001-T09")
        reg = self.register()
        reg.adopt("P-0001-T09", "TASK-009-thing.md", "task")
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("no record in tasks/" in line
                            for line in result["advisories"]))


# ------------------------------------------------- the decisions log (§8.5)

class TestTheDecisionsLog(Temp):
    """§8.5: StageTransition and DecisionRecord are append-only, corrections are
    new records. An earlier session built the record and persisted nothing, on the
    argument that an empty append-only log is not a feature. An overridden gate
    is what finally gives it something to hold."""

    def transition(self, **kwargs):
        _, record, _ = store.transition_project(
            {"ref": "P-0009", "stage": "captured"}, "production",
            override={"by": "ada", "reason": "demo"}, **kwargs)
        return record

    def test_a_transition_is_appended_and_read_back(self):
        record = self.transition(now="2026-09-06T10:00:00Z")
        store.append_decisions([record], self.root / "decisions")
        back = store.read_decisions(self.root / "decisions")
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["override"]["by"], "ada")

    def test_records_are_filed_by_month(self):
        store.append_decisions([self.transition(now="2026-09-06T10:00:00Z"),
                                self.transition(now="2026-10-01T10:00:00Z")],
                               self.root / "decisions")
        names = sorted(p.name for p in (self.root / "decisions").glob("*.jsonl"))
        self.assertEqual(names, ["2026-09.jsonl", "2026-10.jsonl"])

    def test_the_log_refuses_an_edit(self):
        """The same AppendOnlyViolation runs/*.jsonl raises, imported not redefined."""
        path = self.root / "decisions" / "2026-09.jsonl"
        first = self.transition(now="2026-09-06T10:00:00Z")
        store.append_decisions([first], self.root / "decisions")
        with self.assertRaises(store.AppendOnlyViolation):
            store.append_runs(path, [first], existing=[{"kind": "something else"}])

    def test_check_surfaces_an_override_that_stands(self):
        """Evidence nothing reads is filing."""
        store.write_record(self.root / "projects" / "P-0009.md",
                           {"ref": "P-0009", "stage": "production", "name": "x"}, "")
        reg = self.register()
        reg.adopt("P-0009", "x", "project")
        store.append_decisions([self.transition(now="2026-09-06T10:00:00Z")],
                               self.root / "decisions")
        result = store.check(store.load_store(self.root), reg)
        line = [l for l in result["advisories"] if "overridden by ada" in l]
        self.assertEqual(len(line), 1)
        self.assertIn("demo", line[0])
        self.assertEqual(result["problems"], [], "an override is evidence, not a fault")

    def test_the_export_ships_the_log(self):
        store.append_decisions([self.transition(now="2026-09-06T10:00:00Z")],
                               self.root / "decisions")
        payload = store.export(store.load_store(self.root), self.register())
        self.assertEqual(len(payload["decisions"]), 1)


class TestTheTransitionCLI(Temp):

    def setUp(self):
        super().setUp()
        store.write_record(self.root / "projects" / "P-0009.md",
                           {"ref": "P-0009", "stage": "captured", "name": "x",
                            "one_liner": "y"}, "body\n")
        reg = self.register()
        reg.adopt("P-0009", "x", "project")
        reg.save()

    def run_cli(self, *argv):
        import io
        out = io.StringIO()
        code = store.main(["--root", str(self.root), *argv], out=out)
        return code, out.getvalue()

    def test_a_refused_transition_writes_nothing_and_says_what_it_wants(self):
        code, text = self.run_cli("--transition", "P-0009", "--to", "scoped")
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assertIn("act_tier", text)
        self.assertIn("Data sources named", text)      # the exit test, named
        fields, _ = store.read_record(self.root / "projects" / "P-0009.md")
        self.assertEqual(fields["stage"], "captured", "nothing was written")
        self.assertEqual(store.read_decisions(self.root / "decisions"), [])

    def test_an_override_moves_it_and_leaves_a_record(self):
        code, text = self.run_cli("--transition", "P-0009", "--to", "scoped",
                                  "--override-by", "ada",
                                  "--override-reason", "client demo Monday")
        self.assertEqual(code, 0)
        self.assertIn("OVERRIDDEN by ada", text)
        fields, body = store.read_record(self.root / "projects" / "P-0009.md")
        self.assertEqual(fields["stage"], "scoped")
        self.assertIn("body", body, "the body survived the rewrite")
        log = store.read_decisions(self.root / "decisions")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["override"]["reason"], "client demo Monday")

    def test_half_an_override_is_refused_before_the_gate_is_consulted(self):
        code, text = self.run_cli("--transition", "P-0009", "--to", "scoped",
                                  "--override-by", "ada")
        self.assertEqual(code, 1)
        self.assertIn("reason", text)
        self.assertEqual(store.read_decisions(self.root / "decisions"), [])

    def test_a_dry_run_writes_nothing(self):
        code, text = self.run_cli("--transition", "P-0009", "--to", "scoped",
                                  "--override-by", "j", "--override-reason", "r",
                                  "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", text)
        fields, _ = store.read_record(self.root / "projects" / "P-0009.md")
        self.assertEqual(fields["stage"], "captured")

    def test_the_decisions_view_reads_the_log(self):
        self.run_cli("--transition", "P-0009", "--to", "scoped",
                     "--override-by", "ada", "--override-reason", "why")
        code, text = self.run_cli("--decisions")
        self.assertEqual(code, 0)
        self.assertIn("OVERRIDDEN", text)
        self.assertIn("captured -> scoped", text)
