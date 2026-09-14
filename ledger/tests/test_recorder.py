#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s ledger/tests

Plain unittest, no third-party dependencies. Every fixture is written to a
temporary directory; nothing here reads or writes ~/.claude, the archive, or
the network.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recorder  # noqa: E402


# Deliberately unlike any real price table, so a test that passes because it
# quietly picked up the shipped prices.json fails instead.
PRICES = {
    "models": {
        "claude-test-1": {"input": 1.0, "output": 10.0, "cache_write_5m": 100.0,
                          "cache_write_1h": None, "cache_read": 1000.0},
        "claude-test-2": {"input": 2.0, "output": 20.0, "cache_write_5m": 200.0,
                          "cache_write_1h": None, "cache_read": 2000.0},
    },
    "ignored_models": ["<synthetic>"],
}
RAILS = {"default": "subscription", "rules": []}

SESSION = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


def usage(input_=0, output=0, cache_write=0, cache_read=0):
    return {"input_tokens": input_, "output_tokens": output,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read}


def assistant(msg_id, *, session=SESSION, model="claude-test-1",
              timestamp="2026-09-01T10:00:00.000Z", tools=(), stop_reason="end_turn",
              cwd="/Users/x/Projects/demo", entrypoint="claude-desktop", use=None):
    content = [{"type": "text", "text": "hello"}]
    for i, tool in enumerate(tools):
        block = {"type": "tool_use", "id": f"{msg_id}-t{i}",
                 "name": tool["name"], "input": tool.get("input", {})}
        content.append(block)
    return {"type": "assistant", "sessionId": session, "timestamp": timestamp,
            "cwd": cwd, "entrypoint": entrypoint,
            "message": {"id": msg_id, "model": model, "stop_reason": stop_reason,
                        "content": content, "usage": use or usage(output=100)}}


def tool_result(stdout, *, session=SESSION, timestamp="2026-09-01T10:00:01.000Z"):
    return {"type": "user", "sessionId": session, "timestamp": timestamp,
            "toolUseResult": {"stdout": stdout}}


def write_session(directory, name, records, project="demo-project"):
    folder = Path(directory) / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "archive"
        self.source.mkdir()

    def build(self, links=None, stat=None):
        return recorder.build(self.source, PRICES, RAILS, links or {},
                              stat=stat or recorder.stat_output)


# ------------------------------------------------------ output discovery ----

class OutputDiscovery(Fixture):

    def test_write_and_edit_tool_uses_become_outputs_with_their_paths(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Write", "input": {"file_path": "/tmp/a.py"}}]),
            assistant("m2", tools=[{"name": "Edit", "input": {"file_path": "/tmp/b.md"}}]),
        ])
        runs, outputs, _ = self.build(stat=lambda p: {"exists": True, "sha256": "x",
                                                      "bytes": 1, "state": "present_at_record_time"})
        self.assertEqual([o["path"] for o in outputs], ["/tmp/a.py", "/tmp/b.md"])
        self.assertEqual([o["kind"] for o in outputs], ["code", "document"])
        self.assertEqual(runs[0]["outputs"], [o["ref"] for o in outputs])

    def test_a_tool_that_does_not_state_a_path_produces_no_output(self):
        """Bash writes files too. It states a command, not a path, so nothing is
        claimed from it -- guessing a path out of shell is the invention §7 rules
        out. The count is reported instead."""
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Bash",
                                    "input": {"command": "cat > /tmp/secret.py <<'EOF'\nx\nEOF"}}]),
        ])
        runs, outputs, _ = self.build()
        self.assertEqual(outputs, [])
        self.assertEqual(runs[0]["bash_writes_not_claimed_as_outputs"], 1)

    def test_a_read_only_bash_call_is_not_counted_as_a_write(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Bash", "input": {"command": "grep -n foo bar.py"}}]),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["bash_writes_not_claimed_as_outputs"], 0)

    def test_the_same_path_written_five_times_is_one_output(self):
        write_session(self.source, SESSION, [
            assistant(f"m{i}", tools=[{"name": "Write", "input": {"file_path": "/tmp/same.py"}}])
            for i in range(5)
        ])
        _runs, outputs, _ = self.build(
            stat=lambda p: {"exists": True, "sha256": "x", "bytes": 1, "state": "present_at_record_time"})
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["writes_in_run"], 5)

    def test_notebook_edit_uses_its_own_path_key(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "NotebookEdit",
                                    "input": {"notebook_path": "/tmp/n.ipynb"}}]),
        ])
        _runs, outputs, _ = self.build(
            stat=lambda p: {"exists": True, "sha256": "x", "bytes": 1, "state": "present_at_record_time"})
        self.assertEqual(outputs[0]["path"], "/tmp/n.ipynb")


class DeletedAndChangedOutputs(Fixture):

    def test_a_deleted_output_is_recorded_as_gone_not_dropped(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Write",
                                    "input": {"file_path": str(self.root / "vanished.py")}}]),
        ])
        _runs, outputs, _ = self.build()
        self.assertEqual(len(outputs), 1)
        self.assertIs(outputs[0]["exists"], False)
        self.assertIsNone(outputs[0]["sha256"])
        self.assertIsNone(outputs[0]["bytes"])
        self.assertEqual(outputs[0]["state"], "missing_at_record_time")

    def test_a_surviving_output_is_hashed_and_sized_as_at_record_time(self):
        target = self.root / "kept.py"
        target.write_bytes(b"print(1)\n")
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Write", "input": {"file_path": str(target)}}]),
        ])
        _runs, outputs, _ = self.build()
        self.assertIs(outputs[0]["exists"], True)
        self.assertEqual(outputs[0]["bytes"], 9)
        self.assertEqual(len(outputs[0]["sha256"]), 64)

    def test_a_directory_where_a_file_was_is_unreadable_not_a_crash(self):
        target = self.root / "wasafile"
        target.mkdir()
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Write", "input": {"file_path": str(target)}}]),
        ])
        _runs, outputs, _ = self.build()
        self.assertIn("unreadable_at_record_time", outputs[0]["state"])


# ------------------------------------------------- the §7 rule, asserted ----

class DiscoveryReadsTheRecord(unittest.TestCase):
    """§7's correction, pinned as a property of the source file rather than of
    one code path. Timestamp scanning picks up editor saves, formatter runs,
    build artefacts and package installs, and misses nothing only by including
    everything. If a future change reaches for an mtime, this fails."""

    def test_the_module_never_compares_modification_times(self):
        source = (Path(recorder.__file__)).read_text(encoding="utf-8")
        body = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("st_mtime", "getmtime", "st_ctime", "os.walk",
                          "st_birthtime", "iglob", "rglob"):
            self.assertNotIn(forbidden, body,
                             f"{forbidden} in recorder.py: discovery must read the "
                             f"record, never scan the disk (spec-v0.2.md §7)")

    def test_outputs_come_only_from_tools_that_state_a_path(self):
        for tool, key in recorder.WRITE_TOOLS.items():
            self.assertTrue(key.endswith("path"), f"{tool} maps to {key}")


# --------------------------------------------------------------- the run ----

class RunFields(Fixture):

    def test_a_run_with_no_task_ref_says_null_and_says_why(self):
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = self.build()
        self.assertIsNone(runs[0]["task_ref"])
        self.assertIsNone(runs[0]["task_class"])
        self.assertIn("no record links this session to a task",
                      runs[0]["task_link_basis"])

    def test_a_linked_run_carries_the_task_ref_and_its_basis(self):
        write_session(self.source, SESSION, [assistant("m1")])
        links = {SESSION: {"task_ref": "P-0001-T03", "task_class": "code-task",
                           "basis": "run-tasks.json"}}
        runs, _outputs, _ = self.build(links=links)
        self.assertEqual(runs[0]["task_ref"], "P-0001-T03")
        self.assertEqual(runs[0]["task_class"], "code-task")

    def test_status_and_exit_code_are_null_with_a_stated_reason(self):
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = self.build()
        self.assertIsNone(runs[0]["status"])
        self.assertIsNone(runs[0]["exit_code"])
        self.assertIn("not derivable", runs[0]["status_basis"])
        self.assertIn("never executed", runs[0]["exit_code_basis"])

    def test_end_turn_is_the_only_stop_reason_that_maps(self):
        write_session(self.source, SESSION, [
            assistant("m1", timestamp="2026-09-01T10:00:00.000Z", stop_reason="end_turn"),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["stop_reason"], "claude_complete")

    def test_a_run_ending_mid_tool_use_is_not_called_stopped(self):
        """Silence is not evidence. The archive LaunchAgent runs every ten
        minutes, so a transcript ending in tool_use may just be one that has not
        finished arriving."""
        write_session(self.source, SESSION, [
            assistant("m1", timestamp="2026-09-01T10:00:00.000Z", stop_reason="end_turn"),
            assistant("m2", timestamp="2026-09-01T10:05:00.000Z", stop_reason="tool_use"),
        ])
        runs, _outputs, _ = self.build()
        self.assertIsNone(runs[0]["stop_reason"])
        self.assertEqual(runs[0]["api_stop_reason"], "tool_use")
        self.assertIn("not evidence", runs[0]["stop_reason_basis"])

    def test_started_and_ended_span_the_whole_session(self):
        write_session(self.source, SESSION, [
            assistant("m1", timestamp="2026-09-01T10:00:00.000Z"),
            assistant("m2", timestamp="2026-09-01T12:30:00.000Z"),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["started_at"], "2026-09-01T10:00:00.000Z")
        self.assertEqual(runs[0]["ended_at"], "2026-09-01T12:30:00.000Z")

    def test_the_dominant_model_is_the_one_that_produced_the_output(self):
        write_session(self.source, SESSION, [
            assistant("m1", model="claude-test-2", use=usage(output=5)),
            assistant("m2", model="claude-test-1", use=usage(output=500)),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["model"], "claude-test-1")
        self.assertEqual(runs[0]["models"], ["claude-test-1", "claude-test-2"])


class Refs(Fixture):

    def test_the_run_ref_is_derived_from_the_session_id_so_it_never_renumbers(self):
        """A sequential counter assigned in scan order would renumber history
        every time an older transcript reached the archive. An earlier session owns the
        real scheme; this one has to be stable in the meantime."""
        write_session(self.source, SESSION, [assistant("m1")])
        first, _outputs, _ = self.build()
        write_session(self.source, "0000ffff-1111-2222-3333-444455556666",
                      [assistant("m9", session="0000ffff-1111-2222-3333-444455556666",
                                 timestamp="2020-01-01T00:00:00.000Z")])
        second, _outputs, _ = self.build()
        by_session = {r["claude_session_id"]: r["ref"] for r in second}
        self.assertEqual(by_session[SESSION], first[0]["ref"])
        self.assertEqual(len({r["ref"] for r in second}), 2)

    def test_output_refs_are_scoped_to_their_run(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Write", "input": {"file_path": "/tmp/a.py"}}]),
        ])
        runs, outputs, _ = self.build()
        self.assertTrue(outputs[0]["ref"].startswith("O-" + runs[0]["ref"][2:]))
        self.assertEqual(outputs[0]["run_ref"], runs[0]["ref"])


class RailAndSurface(Fixture):

    def test_the_rail_comes_from_rails_json_and_says_when_it_defaulted(self):
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = recorder.build(self.source, PRICES, RAILS, {})
        self.assertEqual(runs[0]["rail"], "subscription")
        self.assertTrue(runs[0]["rail_assumed"])

    def test_a_matching_rails_rule_wins_and_is_not_marked_assumed(self):
        write_session(self.source, SESSION, [assistant("m1", cwd="/Users/x/Projects/demo")])
        rails = {"default": "subscription",
                 "rules": [{"project": "demo", "from": "2026-08-01", "rail": "api"}]}
        runs, _outputs, _ = recorder.build(self.source, PRICES, rails, {})
        self.assertEqual(runs[0]["rail"], "api")
        self.assertFalse(runs[0]["rail_assumed"])

    def test_a_rule_outside_its_date_window_does_not_apply(self):
        write_session(self.source, SESSION, [
            assistant("m1", timestamp="2026-09-01T10:00:00.000Z")])
        rails = {"default": "subscription",
                 "rules": [{"project": "demo", "to": "2026-08-31", "rail": "api"}]}
        runs, _outputs, _ = recorder.build(self.source, PRICES, rails, {})
        self.assertEqual(runs[0]["rail"], "subscription")

    def test_a_known_entrypoint_sets_the_surface_without_assuming(self):
        write_session(self.source, SESSION, [assistant("m1", entrypoint="claude-desktop")])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["surface"], "code")
        self.assertFalse(runs[0]["surface_assumed"])

    def test_an_unknown_entrypoint_defaults_but_is_counted_as_assumed(self):
        write_session(self.source, SESSION, [assistant("m1", entrypoint="something-new")])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["surface"], recorder.DEFAULT_SURFACE)
        self.assertTrue(runs[0]["surface_assumed"])


class UsageAndCost(Fixture):

    def test_cost_is_priced_from_the_four_token_classes(self):
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(input_=1_000_000, output=1_000_000,
                                      cache_write=1_000_000, cache_read=1_000_000)),
        ])
        runs, _outputs, _ = self.build()
        self.assertAlmostEqual(runs[0]["cost_usd"], 1.0 + 10.0 + 100.0 + 1000.0, places=4)
        self.assertEqual(runs[0]["usage"]["input_tokens"], 1_000_000)
        self.assertEqual(runs[0]["usage"]["cache_creation_input_tokens"], 1_000_000)

    def test_a_repeated_message_id_is_counted_once(self):
        """Claude Code writes one line per content block and repeats the usage on
        each. Counting twice inflates every figure downstream."""
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(output=1_000_000)),
            assistant("m1", use=usage(output=1_000_000)),
        ])
        runs, _outputs, _ = self.build()
        self.assertAlmostEqual(runs[0]["cost_usd"], 10.0, places=4)
        self.assertEqual(runs[0]["duplicate_lines_skipped"], 1)

    def test_a_linked_run_that_grew_since_it_was_linked_is_flagged(self):
        """The owner's case: dipping back into an old session, or clicking one in the
        UI, appends to it under its own id. 93264808 went $0.79 -> $1.60 -> $2.76
        that way. It was already a linked run when it moved."""
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(output=1_000_000)),
            assistant("m2", use=usage(output=1_000_000))])
        links = {SESSION: {"task_ref": "P-1", "task_class": "code-task",
                           "basis": "b", "recorded_usd": 10.0,
                           "recorded_messages": 1}}
        runs, _outputs, _ = recorder.build(self.source, PRICES, RAILS, links)
        self.assertTrue(runs[0]["link_drifted"])
        self.assertAlmostEqual(runs[0]["link_drift_usd"], 10.0, places=4)
        self.assertIn("MOVED SINCE IT WAS LINKED", runs[0]["link_drift_basis"])
        self.assertIn("+1 assistant message", runs[0]["link_drift_basis"])

    def test_a_linked_run_that_has_not_moved_is_not_flagged(self):
        write_session(self.source, SESSION, [assistant("m1", use=usage(output=1_000_000))])
        links = {SESSION: {"task_ref": "P-1", "task_class": "code-task",
                           "basis": "b", "recorded_usd": 10.0,
                           "recorded_messages": 1}}
        runs, _outputs, _ = recorder.build(self.source, PRICES, RAILS, links)
        self.assertFalse(runs[0]["link_drifted"])
        self.assertAlmostEqual(runs[0]["link_drift_usd"], 0.0, places=4)

    def test_a_link_with_no_baseline_reports_that_rather_than_no_drift(self):
        """Absence of a baseline is not evidence of stability, and the two must
        not read the same downstream."""
        write_session(self.source, SESSION, [assistant("m1")])
        links = {SESSION: {"task_ref": "P-1", "task_class": "code-task", "basis": "b"}}
        runs, _outputs, _ = recorder.build(self.source, PRICES, RAILS, links)
        self.assertIsNone(runs[0]["link_drifted"])
        self.assertIn("no baseline", runs[0]["link_drift_basis"])

    def test_drift_is_reported_not_corrected(self):
        """The recorder must not quietly rewrite the sample: a prior a gate
        decision was made against has to change visibly or not at all."""
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(output=1_000_000)),
            assistant("m2", use=usage(output=1_000_000))])
        links = {SESSION: {"task_ref": "P-1", "task_class": "code-task",
                           "basis": "b", "recorded_usd": 10.0, "recorded_messages": 1}}
        runs, _outputs, _ = recorder.build(self.source, PRICES, RAILS, links)
        self.assertAlmostEqual(runs[0]["cost_usd"], 20.0, places=4)
        priors = recorder.derive_priors(runs, minimum=1)
        self.assertEqual(priors["code-task"]["samples_usd"], [20.0])

    def test_a_resume_fork_does_not_pay_for_the_prefix_it_inherited(self):
        """Resuming a session copies the whole prior transcript into a new file
        and rewrites sessionId on every line, but the message ids survive.
        Priced naively the shared prefix is charged twice. Observed live on
        2026-09-06: ba753004 and 2d8bea1b shared 339 of 343 records and the
        register carried $13.75 of spend that never happened."""
        fork = "bbbbcccc-dddd-eeee-ffff-000011112222"
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(output=1_000_000),
                      timestamp="2026-09-01T10:00:00.000Z"),
        ])
        write_session(self.source, fork, [
            assistant("m1", session=fork, use=usage(output=1_000_000),
                      timestamp="2026-09-01T10:00:00.000Z"),
            assistant("m2", session=fork, use=usage(output=1_000_000),
                      timestamp="2026-09-01T11:00:00.000Z"),
        ])
        runs, _outputs, _ = self.build()
        by_id = {r["claude_session_id"]: r for r in runs}
        self.assertAlmostEqual(by_id[SESSION]["cost_usd"], 10.0, places=4)
        self.assertAlmostEqual(by_id[fork]["cost_usd"], 10.0, places=4)
        self.assertEqual(by_id[fork]["forked_from"], SESSION)
        self.assertEqual(by_id[fork]["fork_shared_messages"], 1)
        self.assertEqual(by_id[fork]["assistant_messages"], 1)

    def test_the_origin_keeps_the_cost_not_the_fork(self):
        """Whoever started first owns the message. build() sorts by first
        timestamp for exactly this reason, so the answer cannot depend on the
        order the archive happens to be walked in."""
        fork = "bbbbcccc-dddd-eeee-ffff-000011112222"
        write_session(self.source, fork, [
            assistant("m1", session=fork, use=usage(output=1_000_000),
                      timestamp="2026-09-01T10:00:00.000Z"),
        ])
        write_session(self.source, SESSION, [
            assistant("m1", use=usage(output=1_000_000),
                      timestamp="2026-09-01T09:00:00.000Z"),
        ])
        runs, _outputs, _ = self.build()
        by_id = {r["claude_session_id"]: r for r in runs}
        self.assertAlmostEqual(by_id[SESSION]["cost_usd"], 10.0, places=4)
        self.assertAlmostEqual(by_id[fork]["cost_usd"], 0.0, places=4)
        self.assertEqual(by_id[fork]["forked_from"], SESSION)

    def test_a_fork_says_so_in_its_cost_basis(self):
        """A figure that is smaller than the file it came from has to explain
        itself, or the next person re-derives it by hand and gets the old
        number back."""
        fork = "bbbbcccc-dddd-eeee-ffff-000011112222"
        write_session(self.source, SESSION, [
            assistant("m1", timestamp="2026-09-01T10:00:00.000Z")])
        write_session(self.source, fork, [
            assistant("m1", session=fork, timestamp="2026-09-01T10:00:00.000Z"),
            assistant("m2", session=fork, timestamp="2026-09-01T11:00:00.000Z")])
        runs, _outputs, _ = self.build()
        by_id = {r["claude_session_id"]: r for r in runs}
        self.assertIn("RESUME-FORK", by_id[fork]["cost_basis"])
        self.assertIn(SESSION, by_id[fork]["cost_basis"])
        self.assertNotIn("RESUME-FORK", by_id[SESSION]["cost_basis"])

    def test_an_ordinary_run_is_not_marked_as_a_fork(self):
        write_session(self.source, SESSION, [assistant("m1"), assistant("m2")])
        runs, _outputs, _ = self.build()
        self.assertIsNone(runs[0]["forked_from"])
        self.assertEqual(runs[0]["fork_shared_messages"], 0)

    def test_pricing_one_session_alone_still_ignores_the_cross_session_ledger(self):
        """usage_and_cost without `claimed` is the old two-argument behaviour:
        a session priced in isolation, which is what a caller holding one
        transcript wants."""
        records = [assistant("m1", use=usage(output=1_000_000))]
        priced = recorder.usage_and_cost(records, PRICES)
        self.assertAlmostEqual(priced["cost_usd"], 10.0, places=4)
        self.assertIsNone(priced["forked_from"])

    def test_a_session_split_across_two_project_folders_is_one_run(self):
        """A session whose cwd changes mid-run is archived under both project
        folders. It is one Run, and the overlap must not be counted twice."""
        line = assistant("m1", use=usage(output=1_000_000))
        write_session(self.source, SESSION, [line], project="folder-a")
        write_session(self.source, SESSION, [line], project="folder-b")
        runs, _outputs, _ = self.build()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]["transcript_paths"]), 2)
        self.assertAlmostEqual(runs[0]["cost_usd"], 10.0, places=4)

    def test_an_unpriced_model_contributes_zero_and_is_named(self):
        write_session(self.source, SESSION, [
            assistant("m1", model="claude-unknown", use=usage(output=1_000_000)),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["cost_usd"], 0.0)
        self.assertEqual(runs[0]["unpriced_models"], ["claude-unknown"])


class Commits(Fixture):

    def test_a_commit_sha_is_read_out_of_the_record_not_out_of_git(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Bash",
                                    "input": {"command": "git commit -m 'x'"}}]),
            tool_result("[main ad22a2a] x\n 1 file changed"),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["commits"], ["ad22a2a"])

    def test_a_bash_call_that_is_not_a_commit_yields_nothing(self):
        write_session(self.source, SESSION, [
            assistant("m1", tools=[{"name": "Bash", "input": {"command": "git status"}}]),
            tool_result("[main ad22a2a] x"),
        ])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["commits"], [])

    def test_a_run_in_a_folder_that_is_not_a_repository_has_no_commits(self):
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = self.build()
        self.assertEqual(runs[0]["commits"], [])


# --------------------------------------------------------------- resume ----

class Resume(Fixture):

    def test_a_run_can_be_found_by_session_id_or_by_ref(self):
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = self.build()
        self.assertIsNotNone(recorder.find_run(runs, SESSION))
        self.assertIsNotNone(recorder.find_run(runs, runs[0]["ref"]))
        self.assertIsNone(recorder.find_run(runs, "nope"))

    def test_the_resume_payload_hands_back_the_id_and_says_it_is_untested(self):
        """PLAN.md row 1's carry-forward. The id is real; the route is not, and
        will not be until PARKED.md item 8 is decided."""
        write_session(self.source, SESSION, [assistant("m1")])
        runs, _outputs, _ = self.build()
        payload = recorder.resume_payload(runs[0])
        self.assertEqual(payload["resume_args"], ["--resume", SESSION])
        self.assertFalse(payload["verified"])
        self.assertIn("UNVERIFIED", payload["note"])


# --------------------------------------------------------------- priors ----

class Priors(Fixture):

    def runs_with_costs(self, costs, task_class="code-task"):
        return [{"task_class": task_class, "cost_usd": c, "ref": f"R-{i}",
                 "task_ref": None, "started_at": f"2026-09-0{i+1}T00:00:00Z"}
                for i, c in enumerate(costs)]

    def test_priors_are_derived_from_run_costs(self):
        priors = recorder.derive_priors(self.runs_with_costs([5.34, 13.93, 17.0]))
        self.assertEqual(priors["code-task"]["n"], 3)
        self.assertEqual(priors["code-task"]["samples_usd"], [5.34, 13.93, 17.0])
        self.assertTrue(priors["code-task"]["usable"])

    def test_a_class_below_the_minimum_is_derived_but_marked_unusable(self):
        priors = recorder.derive_priors(self.runs_with_costs([5.34, 13.93]))
        self.assertFalse(priors["code-task"]["usable"])

    def test_a_run_with_no_task_class_contributes_nothing(self):
        runs = self.runs_with_costs([5.0, 6.0, 7.0])
        for run in runs:
            run["task_class"] = None
        self.assertEqual(recorder.derive_priors(runs), {})

    def test_every_derived_prior_names_the_runs_behind_it(self):
        priors = recorder.derive_priors(self.runs_with_costs([5.0, 6.0, 7.0]))
        self.assertIn("R-0", priors["code-task"]["basis"])
        self.assertIn("aggregate.py", priors["code-task"]["basis"])

    def test_writing_priors_keeps_a_hand_carried_class_it_cannot_derive(self):
        """Deleting a prior because no session has been linked to its class yet
        would throw away a real measurement."""
        gate = self.root / "gate.json"
        gate.write_text(json.dumps({
            "threshold_usd": 10.0, "min_samples_for_prior": 3,
            "priors": {"html-artifact": {"n": 1, "samples_usd": [3.64],
                                         "basis": "hand-carried"}},
            "defaults": {"permission_mode": "acceptEdits"}}))
        priors = recorder.derive_priors(self.runs_with_costs([5.0, 6.0, 7.0]))
        config = recorder.write_priors(priors, path=gate)
        self.assertEqual(config["priors"]["html-artifact"]["source"], "hand-carried")
        self.assertIn("derived", config["priors"]["code-task"]["source"])
        self.assertEqual(config["threshold_usd"], 10.0)
        self.assertEqual(config["defaults"]["permission_mode"], "acceptEdits")


class Joins(Fixture):

    def test_run_tasks_json_is_read_as_a_map_of_session_id_to_task(self):
        path = self.root / "run-tasks.json"
        path.write_text(json.dumps({"map": {SESSION: {"task_ref": "P-0001-T03",
                                                      "task_class": "code-task"}}}))
        self.assertEqual(recorder.load_run_tasks(path)[SESSION]["task_ref"], "P-0001-T03")

    def test_a_missing_run_tasks_file_is_an_empty_map_not_an_error(self):
        self.assertEqual(recorder.load_run_tasks(self.root / "nope.json"), {})

    def test_the_launcher_record_wins_over_the_hand_written_one(self):
        """The launcher was there at launch; the hand-written file is a
        reconstruction."""
        merged = recorder.merge_links(
            {SESSION: {"task_ref": "P-9999-T99", "task_class": "guess"}},
            {SESSION: {"task_ref": "P-0001-T03", "task_class": "code-task"}})
        self.assertEqual(merged[SESSION]["task_ref"], "P-0001-T03")

    def test_a_launcher_record_with_no_task_ref_does_not_override(self):
        merged = recorder.merge_links(
            {SESSION: {"task_ref": "P-0001-T03", "task_class": "code-task"}},
            {SESSION: {"task_ref": None, "task_class": None}})
        self.assertEqual(merged[SESSION]["task_ref"], "P-0001-T03")

    def test_wrapper_run_records_are_read_from_disk_and_keyed_by_session(self):
        directory = self.root / "runs"
        directory.mkdir()
        (directory / "one.json").write_text(json.dumps({
            "session_id": SESSION, "task_ref": "P-0001-T04", "task_class": "code-task"}))
        (directory / "broken.json").write_text("{not json")
        links = recorder.load_wrapper_runs(directory)
        self.assertEqual(links[SESSION]["task_class"], "code-task")
        self.assertEqual(len(links), 1)

    def test_no_wrapper_runs_directory_is_empty_not_an_error(self):
        self.assertEqual(recorder.load_wrapper_runs(self.root / "nope"), {})


class Kinds(unittest.TestCase):

    def test_kinds_follow_spec_2_4s_vocabulary(self):
        cases = {
            "/x/ledger/recorder.py": "code",
            "/x/app/app.js": "code",
            "/x/spec-v0.2.md": "spec",
            "/x/TASK-004-recorder.md": "spec",
            "/x/wrapper/NOTES.md": "document",
            "/x/nanowiki-content/pages/glossary.md": "wiki",
            "/x/chart.png": "media",
            "/x/rows.csv": "analysis",
        }
        for path, expected in cases.items():
            self.assertEqual(recorder.output_kind(path), expected, path)


if __name__ == "__main__":
    unittest.main()
