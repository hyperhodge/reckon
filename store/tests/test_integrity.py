#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The append-only tripwire -- §8.5. An earlier session, P-0001-T43.

Usage: python3 -m unittest discover -s store/tests

Own fixtures in a temp dir. NOTHING HERE TOUCHES THE REAL runs/, decisions/ OR
risk/, and nothing here writes the real store/integrity.json: every test builds
its own tree and its own config, because a test for a check that records are
never edited must not be able to edit one.

The verdicts are asserted by NAME rather than by count. A check that merely goes
red tells a session that something happened; the whole value of this one is that
it says which of five different things happened, and a test on the count would
pass while the names drifted.
"""

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import integrity        # noqa: E402
import store as S       # noqa: E402


def record(index):
    return {"ref": f"R-{index:06d}", "cost_usd": index, "task_ref": None}


class Tree:
    """A throwaway copy of the three watched folders, with a baseline of its
    own. Built from scratch rather than copied from the real tree."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "store").mkdir()
        self.config_path = integrity.config_for(self.dir)
        for folder in ("runs", "decisions", "risk"):
            (self.dir / folder).mkdir()
        self.write_jsonl("runs/2026-09.jsonl", [record(i) for i in range(1, 6)])
        self.write_jsonl("decisions/2026-09.jsonl", [{"kind": "Transition", "at": "x"}])
        self.snapshot("risk/P-0001-2026-09-06.md", "nine dimensions\n")
        (self.dir / "risk" / "NOTES.md").write_text("prose\n", encoding="utf-8")
        self.config = {
            "hash": "sha256",
            "watched": [
                {"root": "runs", "pattern": "*.jsonl", "kind": "ledger"},
                {"root": "decisions", "pattern": "*.jsonl", "kind": "ledger"},
                {"root": "risk", "pattern": "*.md", "kind": "snapshot"},
            ],
            "not_watched": [{"path": "risk/NOTES.md", "reason": "documentation"}],
            "baseline": {},
        }

    def write_jsonl(self, relative, records):
        path = self.dir / relative
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
                        encoding="utf-8")
        return path

    def snapshot(self, relative, text):
        (self.dir / relative).write_text(text, encoding="utf-8")

    def path(self, relative):
        return self.dir / relative

    def baseline(self, **kwargs):
        self.config, report = integrity.update_baseline(
            self.dir, self.config, now="2026-09-10T00:00:00Z", **kwargs)
        self.save()
        return report

    def save(self):
        integrity.save_config(self.config, self.config_path)

    def verify(self):
        return integrity.verify(self.dir, self.config)

    def verdicts(self, relative=None):
        return sorted(f["verdict"] for f in self.verify()["findings"]
                      if relative is None or f["path"] == relative)


class TestFingerprint(unittest.TestCase):

    def setUp(self):
        self.tree = Tree()

    def test_it_hashes_exactly_the_bytes_it_says_it_hashes(self):
        path = self.tree.path("runs/2026-09.jsonl")
        data = path.read_bytes()
        self.assertEqual(integrity.prefix_hash(path, len(data)),
                         integrity.sha256_of(data))
        self.assertEqual(integrity.prefix_hash(path, 10),
                         integrity.sha256_of(data[:10]))

    def test_a_prefix_longer_than_the_file_is_not_a_hash(self):
        # The file being shorter than the recorded length IS the finding, and
        # returning a hash of what is left would hide it behind a mismatch.
        path = self.tree.path("runs/2026-09.jsonl")
        self.assertIsNone(integrity.prefix_hash(path, path.stat().st_size + 1))

    def test_it_counts_records_and_calls_a_whole_file_complete(self):
        print = integrity.fingerprint(self.tree.path("runs/2026-09.jsonl"))
        self.assertEqual(print["records"], 5)
        self.assertEqual(print["final_line"], "complete")

    def test_a_snapshot_is_fingerprinted_without_a_record_count(self):
        # A .md has no records, and inventing a count for one would be the
        # check claiming to know something it does not.
        print = integrity.fingerprint(self.tree.path("risk/P-0001-2026-09-06.md"),
                                      kind="snapshot")
        self.assertNotIn("records", print)
        self.assertIn("sha256", print)

    def test_a_final_line_with_no_newline_is_an_interrupted_write(self):
        # Even when it parses: append_runs writes a newline per record, so a
        # file that does not end in one was not finished.
        path = self.tree.write_jsonl("runs/2026-09.jsonl", [record(1)])
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        self.assertEqual(integrity.fingerprint(path)["final_line"], "interrupted")

    def test_an_empty_file_is_not_an_interrupted_write(self):
        path = self.tree.path("runs/2026-10.jsonl")
        path.write_text("", encoding="utf-8")
        self.assertEqual(integrity.fingerprint(path)["final_line"], "empty")


class TestTheWatchList(unittest.TestCase):

    def setUp(self):
        self.tree = Tree()

    def test_it_watches_the_three_append_only_folders(self):
        watched = integrity.watched_files(self.tree.dir, self.tree.config)
        self.assertEqual(sorted(watched),
                         ["decisions/2026-09.jsonl", "risk/P-0001-2026-09-06.md",
                          "runs/2026-09.jsonl"])

    def test_not_watched_is_honoured_by_path(self):
        # risk/NOTES.md is prose and is meant to be edited. Holding it to the
        # snapshot rule would make an honest edit read as tampering.
        self.assertNotIn("risk/NOTES.md",
                         integrity.watched_files(self.tree.dir, self.tree.config))

class TestACleanTree(unittest.TestCase):

    def setUp(self):
        self.tree = Tree()
        self.tree.baseline()

    def test_an_untouched_tree_is_clean(self):
        result = self.tree.verify()
        self.assertTrue(result["clean"])
        self.assertEqual(result["problems"], [])

    def test_appending_is_not_a_finding(self):
        # The entire point: the register appends every day and the check must
        # be silent about it, or nobody will read it when it is not.
        path = self.tree.path("runs/2026-09.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(6), sort_keys=True) + "\n")
        self.assertTrue(self.tree.verify()["clean"])

    def test_an_empty_baseline_verifies_nothing_and_says_so(self):
        tree = Tree()
        result = tree.verify()
        self.assertEqual(result["coverage"]["recorded"], 0)
        self.assertEqual(result["coverage"]["bytes_covered"], 0)
        self.assertTrue(all(f["verdict"] == integrity.UNRECORDED
                            for f in result["findings"]))

    def test_a_stale_baseline_still_verifies_the_prefix_it_holds(self):
        # Staleness costs coverage, never correctness -- the recorded prefix is
        # still checked, and an edit inside it is still caught.
        path = self.tree.path("runs/2026-09.jsonl")
        covered = self.tree.config["baseline"]["runs/2026-09.jsonl"]["bytes"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(6), sort_keys=True) + "\n")
        data = bytearray(path.read_bytes())
        data[10] = ord("Z") if data[10] != ord("Z") else ord("Y")
        path.write_bytes(bytes(data))
        result = self.tree.verify()
        self.assertIn(integrity.EDITED, [f["verdict"] for f in result["problems"]])
        self.assertLess(covered, path.stat().st_size)   # the baseline is behind
        self.assertEqual(
            self.tree.config["baseline"]["runs/2026-09.jsonl"]["bytes"], covered)


class TestTheVerdicts(unittest.TestCase):
    """One test per failure, and each asserts the NAME of the verdict."""

    def setUp(self):
        self.tree = Tree()
        self.tree.baseline()

    def test_a_byte_changed_in_the_middle_is_an_edit(self):
        path = self.tree.path("runs/2026-09.jsonl")
        data = bytearray(path.read_bytes())
        middle = len(data) // 2
        data[middle] = ord("0") if data[middle] != ord("0") else ord("1")
        path.write_bytes(bytes(data))
        self.assertIn(integrity.EDITED, self.tree.verdicts("runs/2026-09.jsonl"))

    def test_a_shortened_file_is_a_truncation_and_a_loss(self):
        self.tree.write_jsonl("runs/2026-09.jsonl", [record(i) for i in range(1, 3)])
        verdicts = self.tree.verdicts("runs/2026-09.jsonl")
        self.assertIn(integrity.TRUNCATED, verdicts)
        self.assertIn(integrity.RECORDS_LOST, verdicts)

    def test_a_half_written_last_line_is_an_interrupted_write_by_name(self):
        # THE QUIET FAILURE. A reader that skips this line returns a smaller
        # number and reports no error at all, which is absent data as fact.
        path = self.tree.path("runs/2026-09.jsonl")
        path.write_bytes(path.read_bytes() + b'{"ref": "R-000006", "cost_us')
        findings = [f for f in self.tree.verify()["problems"]
                    if f["verdict"] == integrity.INTERRUPTED_WRITE]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], "runs/2026-09.jsonl")

    def test_an_interrupted_write_is_never_reported_as_a_parse_error(self):
        path = self.tree.path("runs/2026-09.jsonl")
        path.write_bytes(path.read_bytes() + b'{"ref": "R-00000')
        verdicts = self.tree.verdicts("runs/2026-09.jsonl")
        self.assertNotIn(integrity.MALFORMED_LINE, verdicts)
        self.assertIn(integrity.INTERRUPTED_WRITE, verdicts)

    def test_a_damaged_line_that_is_not_the_last_is_its_own_verdict(self):
        # Nothing interrupts the middle of a file, so this is not an
        # interrupted write and calling it one would name the wrong cause.
        path = self.tree.path("runs/2026-09.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = '{"ref": "R-000002", "cost'
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        verdicts = self.tree.verdicts("runs/2026-09.jsonl")
        self.assertIn(integrity.MALFORMED_LINE, verdicts)
        self.assertNotIn(integrity.INTERRUPTED_WRITE, verdicts)

    def test_a_deleted_file_is_missing_and_not_merely_absent(self):
        self.tree.path("risk/P-0001-2026-09-06.md").unlink()
        self.assertIn(integrity.MISSING, self.tree.verdicts("risk/P-0001-2026-09-06.md"))

    def test_a_snapshot_that_grew_is_a_finding_even_though_the_prefix_holds(self):
        # §6: a risk snapshot is append-only as a WHOLE FILE and a correction is
        # a new dated file. This is the one place the check is stricter on a .md
        # than it is on a .jsonl.
        path = self.tree.path("risk/P-0001-2026-09-06.md")
        path.write_text(path.read_text(encoding="utf-8") + "later thought\n",
                        encoding="utf-8")
        self.assertIn(integrity.EXTENDED, self.tree.verdicts("risk/P-0001-2026-09-06.md"))

    def test_a_new_file_is_an_advisory_and_never_a_problem(self):
        # A new month is legitimate and must not read as a loss.
        self.tree.write_jsonl("runs/2026-10.jsonl", [record(9)])
        result = self.tree.verify()
        self.assertEqual([f["verdict"] for f in result["problems"]], [])
        self.assertIn(integrity.UNRECORDED,
                      [f["verdict"] for f in result["advisories"]])

    def test_one_file_can_earn_two_verdicts(self):
        # A mid-line truncation is both a loss and an interrupted write, and
        # reporting only the first would hide the shape of what happened.
        path = self.tree.path("runs/2026-09.jsonl")
        data = path.read_bytes()
        path.write_bytes(data[:len(data) - 20])
        verdicts = self.tree.verdicts("runs/2026-09.jsonl")
        self.assertIn(integrity.TRUNCATED, verdicts)
        self.assertIn(integrity.INTERRUPTED_WRITE, verdicts)

    def test_a_violation_is_a_finding_and_never_an_exception(self):
        path = self.tree.path("runs/2026-09.jsonl")
        path.write_bytes(b"not json at all")
        self.assertFalse(self.tree.verify()["clean"])   # it returned, it did not raise


class TestTheBaseline(unittest.TestCase):

    def setUp(self):
        self.tree = Tree()

    def test_the_first_run_establishes_what_unchanged_means(self):
        report = self.tree.baseline()
        self.assertEqual(len(report["advanced"]), 3)
        self.assertEqual(len(self.tree.config["baseline"]), 3)

    def test_first_seen_is_kept_when_a_file_is_advanced_again(self):
        self.tree.baseline()
        first = self.tree.config["baseline"]["runs/2026-09.jsonl"]["first_seen"]
        with self.tree.path("runs/2026-09.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(6), sort_keys=True) + "\n")
        self.tree.config, _ = integrity.update_baseline(
            self.tree.dir, self.tree.config, now="2026-09-11T00:00:00Z")
        entry = self.tree.config["baseline"]["runs/2026-09.jsonl"]
        self.assertEqual(entry["first_seen"], first)
        self.assertEqual(entry["last_verified"], "2026-09-11T00:00:00Z")

    def test_the_baseline_does_not_move_over_a_violation(self):
        # If it did, the check would report an edit once and then agree with it
        # forever. This refusal is the difference between a tripwire and a log.
        self.tree.baseline()
        path = self.tree.path("runs/2026-09.jsonl")
        recorded = dict(self.tree.config["baseline"]["runs/2026-09.jsonl"])
        path.write_bytes(b'{"ref": "R-000001"}\n')
        report = self.tree.baseline()
        self.assertEqual([r["path"] for r in report["refused"]], ["runs/2026-09.jsonl"])
        self.assertEqual(self.tree.config["baseline"]["runs/2026-09.jsonl"], recorded)

    def test_an_accepted_violation_advances_and_says_so_on_the_entry(self):
        self.tree.baseline()
        self.tree.path("runs/2026-09.jsonl").write_bytes(b'{"ref": "R-000001"}\n')
        report = self.tree.baseline(accepted=["runs/2026-09.jsonl"])
        self.assertEqual(report["refused"], [])
        entry = self.tree.config["baseline"]["runs/2026-09.jsonl"]
        self.assertIn(integrity.TRUNCATED, entry["accepted_over"])

    def test_accepting_one_file_does_not_advance_another(self):
        self.tree.baseline()
        self.tree.path("runs/2026-09.jsonl").write_bytes(b'{"ref": "R-000001"}\n')
        self.tree.snapshot("risk/P-0001-2026-09-06.md", "rewritten\n")
        report = self.tree.baseline(accepted=["runs/2026-09.jsonl"])
        self.assertEqual([r["path"] for r in report["refused"]],
                         ["risk/P-0001-2026-09-06.md"])

    def test_verifying_writes_nothing(self):
        self.tree.baseline()
        before = {path: path.read_bytes() for path in sorted(self.tree.dir.rglob("*.*"))}
        self.tree.verify()
        after = {path: path.read_bytes() for path in sorted(self.tree.dir.rglob("*.*"))}
        self.assertEqual(before, after)

class TestItNeverRepairs(unittest.TestCase):

    def test_the_module_has_no_delete_or_truncate_path(self):
        # Asserted against the source, as purge.py's equivalent is: behaviour
        # only tells you about the paths a test happened to walk down.
        source = (ROOT / "store" / "integrity.py").read_text(encoding="utf-8")
        for forbidden in (".unlink(", "rmtree", "import shutil", "import os",
                          "os.remove", ".truncate(", '"w"', ".write_bytes("):
            self.assertNotIn(forbidden, source,
                             f"integrity.py must have no way to {forbidden}")

    def test_the_only_thing_it_writes_is_its_own_baseline(self):
        source = (ROOT / "store" / "integrity.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("write_text"), 1)


class TestTheCheckSaysIt(unittest.TestCase):
    """The verdict has to reach the place a session is already looking."""

    def setUp(self):
        self.tree = Tree()
        self.tree.baseline()

    def run_cli(self, argv):
        out = StringIO()
        code = S.main(["--root", str(self.tree.dir)] + argv, out=out)
        return code, out.getvalue()

    def test_integrity_alone_reports_clean_and_exits_zero(self):
        code, text = self.run_cli(["--integrity"])
        self.assertEqual(code, 0)
        self.assertIn("append-only holds", text)

    def test_a_violation_is_red_and_named_on_the_line(self):
        path = self.tree.path("runs/2026-09.jsonl")
        data = bytearray(path.read_bytes())
        data[5] = ord("Z") if data[5] != ord("Z") else ord("Y")
        path.write_bytes(bytes(data))
        code, text = self.run_cli(["--integrity"])
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM", text)
        self.assertIn(integrity.EDITED, text)

    def test_accepting_needs_a_name_and_a_reason(self):
        self.tree.path("runs/2026-09.jsonl").write_bytes(b'{"ref": "R-000001"}\n')
        code, text = self.run_cli(["--integrity-accept", "runs/2026-09.jsonl"])
        self.assertEqual(code, 1)
        self.assertIn("--accept-by", text)

    def test_accepting_records_a_decision_that_outlives_the_acceptance(self):
        self.tree.path("runs/2026-09.jsonl").write_bytes(b'{"ref": "R-000001"}\n')
        code, text = self.run_cli(["--integrity-accept", "runs/2026-09.jsonl",
                                   "--accept-by", "Ada", "--accept-reason", "a test"])
        self.assertEqual(code, 0)
        written = S.read_decisions(self.tree.dir / "decisions")
        accepted = [r for r in written if r.get("kind") == integrity.ACCEPTANCE_KIND]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["by"], "Ada")
        self.assertEqual(accepted[0]["paths"], ["runs/2026-09.jsonl"])
        self.assertIn(integrity.TRUNCATED,
                      [f["verdict"] for f in accepted[0]["findings"]])

    def test_accepting_a_path_with_no_finding_writes_nothing(self):
        before = (self.tree.dir / "decisions/2026-09.jsonl").read_bytes()
        code, text = self.run_cli(["--integrity-accept", "runs/2026-09.jsonl",
                                   "--accept-by", "Ada", "--accept-reason", "x"])
        self.assertEqual(code, 1)
        self.assertIn("nothing to accept", text)
        self.assertEqual((self.tree.dir / "decisions/2026-09.jsonl").read_bytes(), before)


class TestAnInterruptedWriteDoesNotTakeTheCheckDown(unittest.TestCase):
    """Before this, a half-written line raised a JSONDecodeError out of
    load_store() -- loud, but it named no verdict and it stopped every other
    check in the file from running."""

    def test_the_reader_names_the_failure_and_the_line(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / "2026-09.jsonl"
        path.write_text('{"ref": "R-000001"}\n{"ref": "R-0000\n', encoding="utf-8")
        with self.assertRaises(S.InterruptedWrite) as caught:
            S.read_runs_file(path)
        self.assertIn("line 2", str(caught.exception))
        self.assertIn("--integrity", str(caught.exception))

    def test_it_is_a_store_error_so_nothing_catching_those_is_surprised(self):
        self.assertTrue(issubclass(S.InterruptedWrite, S.StoreError))


if __name__ == "__main__":
    unittest.main()
