#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Purge with consent -- §8.5. An earlier session (PLAN.md row 8, third item).

Usage: python3 -m unittest discover -s store/tests

Own fixtures in a temp dir. Nothing here reads ~/.claude, the real archive, the
real store or the network, and NOTHING HERE CAN REACH A BINARY OR DELETE A FILE.
The module under test has no delete path at all, which is the point of it, and
`test_the_module_cannot_delete` asserts that against the source rather than
against behaviour -- behaviour only tells you about the paths a test happened to
walk down.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import purge            # noqa: E402
import store as S       # noqa: E402


NOW = "2026-09-06T20:00:00Z"
OLD = "2026-01-01T00:00:00Z"       # comfortably past any sane rule
RECENT = "2026-09-01T00:00:00Z"


def config(**overrides):
    base = json.loads((ROOT / "store" / "purge.json").read_text())
    base.update(overrides)
    return base


def run(session_id, ended_at=OLD, ref=None):
    return {"ref": ref or f"R-{session_id[:8]}", "claude_session_id": session_id,
            "ended_at": ended_at}


def output(path, first_written_at=OLD, ref="O-test-01", exists=True):
    return {"ref": ref, "path": str(path), "kind": "code", "bytes": 10,
            "first_written_at": first_written_at, "exists": exists}


class ExemptSet(unittest.TestCase):
    """§8.5's exemption, asserted ON THE SET.

    `PARKED.md` item 14 asked for exactly this and named the reason: an
    exemption that is only a branch inside a loop is one refactor away from not
    existing, and the thing being protected is the append-only record of what
    this project did.
    """

    EXEMPT = ["projects", "tasks", "runs", "risk", "decisions", "store/refs.json"]

    def test_every_exempt_path_is_in_the_set(self):
        resolved = purge.exempt_paths(config(), root=ROOT)
        for entry in self.EXEMPT:
            self.assertIn((ROOT / entry).resolve(), resolved,
                          f"{entry} must be in the exempt set, not merely skipped")

    def test_the_set_is_exactly_the_documented_six(self):
        # If someone adds a seventh, they have to come here and say so.
        self.assertEqual(len(purge.exempt_paths(config(), root=ROOT)), 6)

    def test_exempt_paths_and_everything_under_them_are_protected(self):
        conf = config()
        for entry in self.EXEMPT:
            self.assertTrue(purge.is_protected(ROOT / entry, conf, ROOT), entry)
            self.assertTrue(
                purge.is_protected(ROOT / entry / "anything" / "deep.jsonl",
                                   conf, ROOT),
                f"a file inside {entry} must be protected too")

    def test_the_candidate_set_can_never_contain_an_exempt_path(self):
        """The assertion the brief asked for, on the SET.

        Every exempt path is fed in as an Output record, aged far past the rule,
        so the only thing standing between it and the queue is the exemption.
        """
        conf = config()
        outputs = [output(ROOT / entry / "would-be-deleted.jsonl",
                          ref=f"O-{index:02d}")
                   for index, entry in enumerate(self.EXEMPT)]
        outputs.append(output(ROOT / "store" / "refs.json", ref="O-99"))
        manifest = purge.plan(config=conf, runs=[], outputs=outputs,
                              run_tasks={}, gate={}, now=NOW, root=ROOT,
                              archive_root=Path(tempfile.gettempdir()) / "nope")
        eligible = {c["path"] for c in manifest["candidates"]
                    if c["status"] == purge.ELIGIBLE}
        self.assertEqual(eligible, set(),
                         "an exempt path reached the queue")
        for candidate in manifest["candidates"]:
            self.assertEqual(candidate["status"], purge.HELD)

    def test_claude_projects_is_never_a_candidate_root(self):
        """CLAUDE.md's standing rule, and `PARKED.md` item 5's actual cause."""
        conf = config()
        source_tree = Path("~/.claude/projects").expanduser()
        self.assertTrue(purge.is_protected(source_tree, conf, ROOT))
        self.assertTrue(purge.is_protected(
            source_tree / "-Users-ada-x" / "abc.jsonl", conf, ROOT))
        self.assertNotIn(
            str(source_tree),
            json.dumps(conf["candidate_roots"]),
            "the source tree must never be named as a candidate root")


class NoMtimeAndNoDelete(unittest.TestCase):
    """Two source-level assertions.

    `recorder.py` carries a test asserting no mtime call appears in it at all,
    on the argument that a timestamp is a fact about a filesystem and not about
    what happened. The reasoning transfers exactly: ageing a record out of
    existence on an mtime would delete history because someone ran `touch`.
    """

    SOURCE = (ROOT / "store" / "purge.py").read_text()

    def test_no_mtime_anywhere(self):
        for banned in ("st_mtime", "getmtime", "st_ctime", "st_atime",
                       "os.utime"):
            self.assertNotIn(banned, self.SOURCE,
                             f"{banned}: disuse is read from a named field of a "
                             f"record, never from the filesystem")

    def test_the_module_cannot_delete(self):
        for banned in ("os.remove", "os.unlink", ".unlink(", "shutil.rmtree",
                       "os.rmdir", "rmtree"):
            self.assertNotIn(banned, self.SOURCE,
                             f"{banned}: the tool queues and records; the rm is "
                             f"a human's (PARKED.md item 14)")

    def test_the_module_cannot_reach_a_binary(self):
        for banned in ("subprocess", "os.system", "os.exec", "popen"):
            self.assertNotIn(banned, self.SOURCE,
                             f"{banned}: nothing here executes anything")

    def test_the_manifest_says_so_in_the_data(self):
        manifest = purge.plan(config=config(), runs=[], outputs=[],
                              run_tasks={}, gate={}, now=NOW, root=ROOT,
                              archive_root=Path(tempfile.gettempdir()) / "nope")
        self.assertTrue(manifest["deletes_nothing"])


class AgeingFromANamedField(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "archive"
        (self.archive / "proj").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def transcript(self, session_id):
        path = self.archive / "proj" / f"{session_id}.jsonl"
        path.write_text("{}\n")
        return path

    def plan(self, runs, **kwargs):
        return purge.plan(config=kwargs.pop("config", config()), runs=runs,
                          outputs=kwargs.pop("outputs", []),
                          run_tasks=kwargs.pop("run_tasks", {}),
                          gate=kwargs.pop("gate", {}), now=NOW, root=ROOT,
                          archive_root=self.archive, **kwargs)

    def only(self, manifest, path):
        matches = [c for c in manifest["candidates"] if c["path"] == str(path)]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_a_transcript_ages_by_ended_at_on_its_run(self):
        path = self.transcript("aaaaaaaa-0000-0000-0000-000000000000")
        candidate = self.only(
            self.plan([run("aaaaaaaa-0000-0000-0000-000000000000", OLD)]), path)
        self.assertEqual(candidate["status"], purge.ELIGIBLE)
        self.assertEqual(candidate["last_used_field"], "ended_at")
        self.assertIn("ended_at", candidate["reason"])

    def test_a_recent_transcript_is_too_recent_not_eligible(self):
        path = self.transcript("bbbbbbbb-0000-0000-0000-000000000000")
        candidate = self.only(
            self.plan([run("bbbbbbbb-0000-0000-0000-000000000000", RECENT)]), path)
        self.assertEqual(candidate["status"], purge.TOO_RECENT)

    def test_a_transcript_with_no_run_record_is_undecidable_not_eligible(self):
        """The module refusing to guess, which is an earlier session's RI-10 shape."""
        path = self.transcript("cccccccc-0000-0000-0000-000000000000")
        candidate = self.only(self.plan([]), path)
        self.assertEqual(candidate["status"], purge.UNDECIDABLE)
        self.assertIsNone(candidate["last_used_at"])
        self.assertIn("not the same question", candidate["reason"])

    def test_the_boundary_is_inclusive_and_stated(self):
        conf = config(age_days=10)
        path = self.transcript("dddddddd-0000-0000-0000-000000000000")
        at_ten = self.only(self.plan(
            [run("dddddddd-0000-0000-0000-000000000000", "2026-08-27T20:00:00Z")],
            config=conf), path)
        self.assertEqual(at_ten["age_days"], 10)
        self.assertEqual(at_ten["status"], purge.ELIGIBLE)
        at_nine = self.only(self.plan(
            [run("dddddddd-0000-0000-0000-000000000000", "2026-08-28T20:00:00Z")],
            config=conf), path)
        self.assertEqual(at_nine["status"], purge.TOO_RECENT)

    def test_a_transcript_cited_in_run_tasks_is_held(self):
        session = "eeeeeeee-0000-0000-0000-000000000000"
        path = self.transcript(session)
        manifest = self.plan([run(session, OLD)],
                             run_tasks={"map": {session: {"task_ref": "P-0001-T05"}}})
        candidate = self.only(manifest, path)
        self.assertEqual(candidate["status"], purge.HELD)
        self.assertIn("P-0001-T05", candidate["reason"])

    def test_a_transcript_behind_a_derived_prior_is_held(self):
        session = "ffffffff-0000-0000-0000-000000000000"
        path = self.transcript(session)
        gate = {"priors": {"code-task": {"basis": "Derived: R-ffffffff $9.00"}}}
        candidate = self.only(self.plan([run(session, OLD)], gate=gate), path)
        self.assertEqual(candidate["status"], purge.HELD)
        self.assertIn("code-task", candidate["reason"])

    def test_a_working_file_ages_by_first_written_at(self):
        target = Path(self.tmp.name) / "scratch.py"
        manifest = self.plan([], outputs=[output(target, OLD)])
        candidate = self.only(manifest, target)
        self.assertEqual(candidate["status"], purge.ELIGIBLE)
        self.assertEqual(candidate["last_used_field"], "first_written_at")

    def test_a_working_file_with_no_date_is_undecidable(self):
        target = Path(self.tmp.name) / "dateless.py"
        manifest = self.plan([], outputs=[output(target, first_written_at=None)])
        self.assertEqual(self.only(manifest, target)["status"], purge.UNDECIDABLE)

    def test_a_file_no_output_record_names_is_never_a_candidate(self):
        """Walking the tree and ageing what is found would be the mtime
        heuristic wearing a different hat."""
        stray = Path(self.tmp.name) / "nobody-recorded-me.py"
        stray.write_text("x")
        manifest = self.plan([], outputs=[])
        self.assertNotIn(str(stray), [c["path"] for c in manifest["candidates"]])


class Consent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "archive" / "proj"
        self.archive.mkdir(parents=True)
        self.session = "12345678-0000-0000-0000-000000000000"
        (self.archive / f"{self.session}.jsonl").write_text("{}\n")
        self.manifest = purge.plan(
            config=config(), runs=[run(self.session, OLD)], outputs=[],
            run_tasks={}, gate={}, now=NOW, root=ROOT,
            archive_root=self.archive.parent)

    def test_a_consent_needs_a_name(self):
        with self.assertRaises(S.ConsentNotRecorded) as caught:
            purge.consent_record(self.manifest, by="", reason="tidying")
        self.assertIn("--consent-by", str(caught.exception))

    def test_a_consent_needs_a_reason(self):
        with self.assertRaises(S.ConsentNotRecorded) as caught:
            purge.consent_record(self.manifest, by="ada", reason="  ")
        self.assertIn("--consent-reason", str(caught.exception))

    def test_the_record_folds_the_rule_and_the_basis_in_whole(self):
        record = purge.consent_record(self.manifest, "ada", "archive tidy",
                                      at=NOW)
        self.assertEqual(record["kind"], "PurgeConsent")
        self.assertEqual(record["rule"]["age_days"], config()["age_days"])
        self.assertEqual(len(record["queued_paths"]), 1)
        basis = record["queued_basis"][0]
        self.assertEqual(basis["last_used_field"], "ended_at")
        self.assertEqual(basis["last_used_at"], OLD)

    def test_the_record_does_not_claim_anything_was_deleted(self):
        record = purge.consent_record(self.manifest, "ada", "archive tidy")
        self.assertFalse(record["executed_by_this_tool"])
        self.assertIn("does not know", record["executed_by_this_tool_basis"])

    def test_no_ref_is_allocated_for_a_log_entry(self):
        """An earlier session's reasoning unchanged: §2 gives refs to Projects, Tasks and
        Runs and none to a log entry."""
        record = purge.consent_record(self.manifest, "ada", "archive tidy")
        self.assertNotIn("ref", record)

    def test_the_consent_lands_in_the_append_only_decision_log(self):
        decisions = Path(self.tmp.name) / "decisions"
        decisions.mkdir()
        record = purge.consent_record(self.manifest, "ada", "tidy", at=NOW)
        S.append_decisions([record], decisions)
        S.append_decisions([dict(record, at="2026-09-07T00:00:00Z")], decisions)
        found = purge.purge_consents(decisions)
        self.assertEqual(len(found), 2, "the log is append-only, not replaced")

    def test_disclosive_fields_are_named_so_they_can_be_redacted(self):
        """An earlier session's rule: a disclosive value must live in a NAMED FIELD or it
        cannot be redacted. `path` and `reason` are both, and `queued_paths` is
        named rather than folded into a formatted sentence."""
        record = purge.consent_record(self.manifest, "ada", "tidy")
        for field in ("queued_paths", "queued_basis", "reason"):
            self.assertIn(field, record)
            self.assertIn(field, purge.DISCLOSIVE_PURGE_FIELDS)


class TheCommandIsPrintedNotRun(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "a r c h i v e" / "proj"
        self.archive.mkdir(parents=True)
        self.session = "87654321-0000-0000-0000-000000000000"
        (self.archive / f"{self.session}.jsonl").write_text("{}\n")

    def manifest(self, runs):
        return purge.plan(config=config(), runs=runs, outputs=[], run_tasks={},
                          gate={}, now=NOW, root=ROOT,
                          archive_root=self.archive.parent)

    def test_paths_with_spaces_are_quoted(self):
        """This project's own directories have spaces in them, so an unquoted
        path is not a style point."""
        command = purge.command_for(self.manifest([run(self.session, OLD)]))
        self.assertIn("'", command)
        self.assertIn("rm -i --", command)
        self.assertIn("a r c h i v e", command)

    def test_one_rm_per_line(self):
        command = purge.command_for(self.manifest([run(self.session, OLD)]))
        self.assertEqual(sum(1 for line in command.splitlines()
                             if line.startswith("rm ")), 1)

    def test_nothing_eligible_means_no_command(self):
        self.assertIsNone(purge.command_for(self.manifest([run(self.session, RECENT)])))

    def test_the_file_is_still_there_afterwards(self):
        """The whole design in one assertion."""
        target = self.archive / f"{self.session}.jsonl"
        manifest = self.manifest([run(self.session, OLD)])
        purge.command_for(manifest)
        purge.consent_record(manifest, "ada", "tidy")
        self.assertTrue(target.exists())


class DryRunIsTheDefault(unittest.TestCase):
    """As `wrapper/launch.py` does. Here it is the only run: what the flag gates
    is whether a consent is RECORDED, because there is nothing else to gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "decisions").mkdir()
        (self.root / "runs").mkdir()
        (self.root / "ledger").mkdir()
        (self.root / "wrapper").mkdir()
        (self.root / "ledger" / "runs.json").write_text('{"outputs": []}')
        (self.root / "ledger" / "run-tasks.json").write_text("{}")
        (self.root / "wrapper" / "gate.json").write_text("{}")

    def out(self, argv):
        import io
        buffer = io.StringIO()
        code = purge.main(argv + ["--root", str(self.root)], out=buffer)
        return code, buffer.getvalue()

    def test_default_writes_nothing(self):
        code, text = self.out([])
        self.assertEqual(code, 0)
        self.assertIn("dry run", text)
        self.assertEqual(list((self.root / "decisions").glob("*.jsonl")), [])

    def test_a_consent_without_a_name_refuses_and_writes_nothing(self):
        code, text = self.out(["--consent-reason", "tidy"])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", text)
        self.assertEqual(list((self.root / "decisions").glob("*.jsonl")), [])

    def test_the_consent_log_reads_back_empty_before_anything_is_recorded(self):
        code, text = self.out(["--consent-log"])
        self.assertEqual(code, 0)
        self.assertIn("no purge has been consented to", text)


if __name__ == "__main__":
    unittest.main()
