#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s wrapper/tests

Nothing here executes `claude`, and nothing here touches the real environment:
every test passes its own env dict and its own which() so the result does not
depend on what happens to be installed on the machine running it.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate  # noqa: E402
import launch  # noqa: E402


TASK = """---
ref: P-0001-T03
project: P-0001 (P2P Register)
state: approved
approved_by: ada
approved_at: 2026-09-05T15:40:00Z
task_type: execute
est_p95_usd: 6.00
---

# TASK-003 — body text, which is the prompt.
"""

GATE_CONFIG = {
    "threshold_usd": 10.0,
    "min_samples_for_prior": 3,
    "api_rail_cap_ceiling_usd": 5.0,
    "priors": {},
    "defaults": {"permission_mode": "acceptEdits",
                 "allowed_tools": ["Read", "Edit", "Write", "Bash"],
                 "output_format": "json"},
}


class Fixture:
    """A task file and a gate.json in a temporary directory."""

    def __init__(self, stack, task=TASK, config=None):
        tmp = Path(tempfile.mkdtemp())
        stack.append(tmp)
        self.dir = tmp
        self.task = tmp / "TASK-003-wrapper.md"
        self.task.write_text(task)
        self.config = tmp / "gate.json"
        self.config.write_text(json.dumps(config or GATE_CONFIG))


class FrontMatter(unittest.TestCase):
    def test_parses_the_shape_the_task_files_use(self):
        front, body = launch.parse_front_matter(TASK)
        self.assertEqual(front["ref"], "P-0001-T03")
        self.assertEqual(front["state"], "approved")
        self.assertEqual(front["est_p95_usd"], "6.00")
        self.assertIn("body text", body)

    def test_colons_in_the_value_survive(self):
        front, _ = launch.parse_front_matter(
            "---\napproved_at: 2026-09-05T15:40:00Z\n---\nx\n")
        self.assertEqual(front["approved_at"], "2026-09-05T15:40:00Z")

    def test_quotes_are_stripped(self):
        front, _ = launch.parse_front_matter('---\nref: "P-0001-T03"\n---\n')
        self.assertEqual(front["ref"], "P-0001-T03")

    def test_no_front_matter_is_an_error_not_an_empty_dict(self):
        with self.assertRaises(launch.FrontMatterError):
            launch.parse_front_matter("# Just a heading\n")

    def test_unclosed_front_matter_is_an_error(self):
        with self.assertRaises(launch.FrontMatterError):
            launch.parse_front_matter("---\nref: X\n\n# body\n")

    def test_nested_yaml_is_rejected_rather_than_half_parsed(self):
        with self.assertRaises(launch.FrontMatterError):
            launch.parse_front_matter("---\nbudget:\n  usd: 6\n---\n")

    def test_a_line_without_a_colon_is_rejected(self):
        with self.assertRaises(launch.FrontMatterError):
            launch.parse_front_matter("---\nref: X\njust a sentence\n---\n")

    def test_a_repeated_key_is_rejected(self):
        with self.assertRaises(launch.FrontMatterError):
            launch.parse_front_matter("---\nest_p95_usd: 6\nest_p95_usd: 60\n---\n")

class RailDetection(unittest.TestCase):
    def test_no_key_is_the_subscription_rail(self):
        rail, warning = launch.detect_rail({})
        self.assertEqual(rail, "subscription")
        self.assertIsNone(warning)

    def test_empty_key_is_not_a_key(self):
        rail, _ = launch.detect_rail({"ANTHROPIC_API_KEY": "   "})
        self.assertEqual(rail, "subscription")

    def test_a_key_moves_the_run_to_the_api_rail_and_warns(self):
        rail, warning = launch.detect_rail({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
        self.assertEqual(rail, "api")
        self.assertIn("list price", warning)
        self.assertIn("rails.json", warning)

    def test_the_warning_never_offers_to_write_a_rails_rule(self):
        _, warning = launch.detect_rail({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
        self.assertIn("will not write one for you", warning)


class BinaryDiscovery(unittest.TestCase):
    def test_found_on_path(self):
        found = launch.find_claude_binary({}, which=lambda name: "/fake/bin/claude")
        self.assertEqual(found, "/fake/bin/claude")

    def test_missing_returns_none_rather_than_a_guessed_path(self):
        """Absence is injected, not assumed. Passing candidates=[] states that
        nothing is at the fallback locations; before 6 September this test
        passed because nothing actually was, and it broke the day a CLI was
        installed at ~/.local/bin/claude."""
        self.assertIsNone(launch.find_claude_binary(
            {}, which=lambda name: None, candidates=[]))

    def test_the_real_fallback_list_is_still_the_default(self):
        """The injection must not quietly change what the launcher looks at."""
        self.assertIs(launch.find_claude_binary.__defaults__[-1], None)
        self.assertIn("~/.claude/local/claude", launch.FALLBACK_BINARY_PATHS)

    def test_claude_cli_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\n")
            fake.chmod(0o755)
            found = launch.find_claude_binary({"CLAUDE_CLI": str(fake)},
                                              which=lambda name: None)
            self.assertEqual(found, str(fake))

    def test_a_claude_cli_pointing_at_nothing_is_still_none(self):
        self.assertIsNone(launch.find_claude_binary({"CLAUDE_CLI": "/nope/claude"},
                                                    which=lambda name: None))


class Command(unittest.TestCase):
    def argv(self, **kw):
        return launch.build_command("/fake/bin/claude", Path("spec.md"), "PROMPT",
                                    allowed_tools=["Read", "Edit"],
                                    permission_mode="acceptEdits", **kw)

    def test_the_seven_shape(self):
        argv = self.argv()
        self.assertEqual(argv[:3], ["/fake/bin/claude", "-p", "PROMPT"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Edit")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_permission_mode_is_always_passed(self):
        self.assertIn("--permission-mode", self.argv())

    def test_the_flag_is_session_id_not_session(self):
        argv = self.argv(session_id="6f1c9a1e-0000-4000-8000-000000000000")
        self.assertIn("--session-id", argv)
        self.assertNotIn("--session", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1],
                         "6f1c9a1e-0000-4000-8000-000000000000")

    def test_it_refuses_to_build_without_a_binary(self):
        with self.assertRaises(ValueError):
            launch.build_command(None, Path("spec.md"), "PROMPT",
                                 allowed_tools=["Read"], permission_mode="acceptEdits")

    def test_render_shows_the_prompt_as_cat_not_the_whole_file(self):
        rendered = launch.render_command(self.argv(), Path("specs/T03.md"))
        self.assertIn('"$(cat specs/T03.md)"', rendered)
        self.assertNotIn("PROMPT", rendered)


class RunRecord(unittest.TestCase):
    def record(self, rail="subscription", result=None):
        front, _ = launch.parse_front_matter(TASK)
        decision = gate.decide(front, GATE_CONFIG)
        return launch.run_record(task_path=Path("TASK-003-wrapper.md"),
                                 front_matter=front, decision=decision, rail=rail,
                                 session_id="s-1", command="claude -p ...",
                                 result=result)

    def test_the_rail_is_recorded_on_the_run_with_its_basis(self):
        record = self.record(rail="api")
        self.assertEqual(record["rail"], "api")
        self.assertIn("ANTHROPIC_API_KEY", record["rail_basis"])

    def test_cost_comes_from_claudes_own_json_and_is_labelled(self):
        record = self.record(result={"total_cost_usd": 4.21, "session_id": "s-1"})
        self.assertEqual(record["total_cost_usd"], 4.21)
        self.assertEqual(record["cost_source"], "claude --output-format json")

    def test_no_result_means_no_cost_rather_than_zero(self):
        self.assertIsNone(self.record()["total_cost_usd"])

    def test_a_returned_session_id_that_differs_is_flagged_not_overwritten(self):
        record = self.record(result={"total_cost_usd": 1.0, "session_id": "s-2"})
        self.assertTrue(record["session_id_mismatch"])
        self.assertEqual(record["session_id"], "s-1")
        self.assertEqual(record["session_id_returned"], "s-2")


class Cli(unittest.TestCase):
    """End to end through main(), with a fake env. Never executes anything --
    and as of 6 September that is enforced rather than hoped for.

    It used to read "the one case that would reach subprocess.run has no binary
    to run", which was true of this machine and not of this test. A `claude` was
    installed on 6 September and the refusal test executed it for real. Every
    case now runs with CLAUDE_CLI pointed at a path that cannot exist, so
    find_claude_binary returns None by construction; a test that wants a binary
    passes its own env and its own fake."""

    NO_BINARY = "/nonexistent/p2p-register-test/claude"

    def setUp(self):
        self.dirs = []
        self.fix = Fixture(self.dirs)

    def tearDown(self):
        import shutil
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def run_main(self, args=(), env=None, fix=None):
        fix = fix or self.fix
        out = io.StringIO()
        # The decisions log is redirected into the fixture's own directory. The
        # real decisions/*.jsonl is canonical and append-only; a test suite must
        # not be able to write to it any more than it can spend money.
        code = launch.main([str(fix.task), "--gate-config", str(fix.config), *args],
                           env={"CLAUDE_CLI": self.NO_BINARY} if env is None else env,
                           out=out, decisions_dir=fix.dir / "decisions")
        return code, out.getvalue()

    def decisions(self, fix=None):
        directory = (fix or self.fix).dir / "decisions"
        return [json.loads(line)
                for path in sorted(directory.glob("*.jsonl"))
                for line in path.read_text().splitlines() if line.strip()]

    def test_dry_run_is_the_default_and_runs_nothing(self):
        code, text = self.run_main()
        self.assertEqual(code, launch.EXIT_OK)
        self.assertIn("ALLOW", text)
        self.assertIn("Dry run. Nothing was run.", text)

    def test_the_command_is_printed_when_a_binary_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            code, text = self.run_main(env={"CLAUDE_CLI": str(fake)})
        self.assertEqual(code, launch.EXIT_OK)
        self.assertIn("-p \"$(cat", text)
        self.assertIn("--session-id", text)
        self.assertIn("--permission-mode acceptEdits", text)

    def test_dry_run_beats_execute_when_both_are_given(self):
        """The unsafe flag must never be the one that silently wins."""
        code, text = self.run_main(["--dry-run", "--execute"])
        self.assertEqual(code, launch.EXIT_OK)
        self.assertIn("--dry-run wins", text)
        self.assertIn("Nothing was run", text)
        self.assertNotIn("REFUSING TO EXECUTE", text)

    def test_the_default_fixture_can_never_find_a_real_binary(self):
        """The guard on the guard. If this fails, every refusal test in this
        class is capable of spending money."""
        self.assertFalse(Path(self.NO_BINARY).exists())
        self.assertIsNone(launch.find_claude_binary({"CLAUDE_CLI": self.NO_BINARY},
                                                    which=lambda name: None))

    def test_execute_refuses_when_the_binary_is_missing(self):
        code, text = self.run_main(["--execute"])
        self.assertEqual(code, launch.EXIT_ERROR)
        self.assertIn("REFUSING TO EXECUTE", text)
        self.assertIn("no `claude` binary was found", text)
        self.assertIn("fails silently", text)

    def test_the_missing_binary_message_names_where_it_looked(self):
        _, text = self.run_main()
        self.assertIn("~/.claude/local/claude", text)
        self.assertIn("CLAUDE_CLI", text)

    def test_a_dry_run_with_no_binary_shows_the_shape_labelled_unverified(self):
        """Useful on this machine, where there is no CLI, without ever calling
        an unchecked command line a command."""
        _, text = self.run_main()
        self.assertIn("shape    claude -p \"$(cat", text)
        self.assertIn("UNVERIFIED", text)
        self.assertNotIn("command  claude", text)

    # ------------------------------------------- the api rail, TASK-012 --
    # An API key used to warn loudly and downgrade an allow to an ask. Since
    # 6 September it blocks: an ask is answerable at 07:00 by nobody, and the
    # rail bills real money at list price on a subscription with auto-payments
    # off. It passes only on a per-run opt-in written on the task file, and
    # even then only as far as an ask.

    API_ENV = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CLI": NO_BINARY}

    OPT_IN = ("api_rail_allowed_by: Ada\n"
              "api_rail_reason: one-off reproducibility check\n"
              "api_rail_cap_usd: 4.00\n"
              "est_p95_usd: 3.00\n")

    def opted_in(self, block=OPT_IN, **kw):
        return Fixture(self.dirs,
                       task=TASK.replace("est_p95_usd: 6.00\n", block), **kw)

    def test_an_api_key_blocks_and_warns_loudly(self):
        code, text = self.run_main(env=self.API_ENV)
        self.assertEqual(code, launch.EXIT_BLOCK)
        self.assertIn("RAIL WARNING", text)
        self.assertIn("rail     api", text)
        self.assertIn("BLOCKED. Nothing was run.", text)
        self.assertIn("api_rail_allowed_by", text)

    def test_execute_on_the_api_rail_stops_at_the_gate(self):
        code, text = self.run_main(["--execute"], env=self.API_ENV)
        self.assertEqual(code, launch.EXIT_BLOCK)
        self.assertNotIn("REFUSING TO EXECUTE", text)

    def test_a_block_records_no_opt_in(self):
        self.run_main(env=self.API_ENV)
        self.assertEqual(self.decisions(), [])

    def test_a_complete_opt_in_asks_and_is_recorded(self):
        fix = self.opted_in()
        code, text = self.run_main(env=self.API_ENV, fix=fix)
        self.assertEqual(code, launch.EXIT_ASK)
        self.assertIn("ASK. Nothing was run.", text)
        self.assertIn("opt-in recorded", text)
        records = self.decisions(fix)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "ApiRailOptIn")
        self.assertEqual(records[0]["override"]["by"], "Ada")
        self.assertNotIn("task_path", records[0])
        self.assertEqual(records[0]["cap_usd"], 4.00)
        self.assertEqual(records[0]["gate_action"], "ask")
        self.assertEqual(records[0]["task_ref"], "P-0001-T03")

    def test_the_recorded_opt_in_redacts_under_presentation_unchanged(self):
        """The record reuses store.py's override shape, so governance.py's
        existing redactor covers it without being told about it. Presentation
        mode is the export's, not the screen's (RI-08), and the free text a
        person wrote about a spend is what it drops."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "store"))
        import governance  # noqa: E402

        fix = self.opted_in()
        self.run_main(env=self.API_ENV, fix=fix)
        record = self.decisions(fix)[0]
        presented = governance._redact_decision(record)
        self.assertEqual(presented["override"]["by"], "Ada")
        self.assertNotEqual(presented["override"]["reason"], record["override"]["reason"])
        self.assertNotIn("one-off reproducibility check", json.dumps(presented))

    def test_either_spelling_of_the_project_reference_is_recorded(self):
        """tasks/ writes `project_ref: P-0001`; the hand-written briefs write
        `project: P-0001 (P2P Register)`. Both, and neither, must survive --
        the first cut read only `project` and raised IndexError on a real task
        file, turning the whole launch into an exit 1."""
        for line in ("project_ref: P-0001", "project: P-0001 (P2P Register)", ""):
            with self.subTest(line=line):
                fix = self.opted_in()
                fix.task.write_text(fix.task.read_text().replace(
                    "project: P-0001 (P2P Register)", line))
                self.assertEqual(self.run_main(env=self.API_ENV, fix=fix)[0],
                                 launch.EXIT_ASK)
                expected = "P-0001" if line else None
                self.assertEqual(self.decisions(fix)[0]["project_ref"], expected)

    def test_the_decisions_log_is_append_only_across_invocations(self):
        fix = self.opted_in()
        self.run_main(env=self.API_ENV, fix=fix)
        self.run_main(env=self.API_ENV, fix=fix)
        self.assertEqual(len(self.decisions(fix)), 2)

    def test_a_cap_above_the_ceiling_blocks_and_records_nothing(self):
        fix = self.opted_in(self.OPT_IN.replace("4.00", "80.00"))
        code, text = self.run_main(env=self.API_ENV, fix=fix)
        self.assertEqual(code, launch.EXIT_BLOCK)
        self.assertIn("api_rail_cap_ceiling_usd", text)
        self.assertEqual(self.decisions(fix), [])

    def test_the_subscription_rail_is_untouched_by_the_opt_in_fields(self):
        fix = self.opted_in()
        code, text = self.run_main(fix=fix)
        self.assertEqual(code, launch.EXIT_OK)
        self.assertIn("rail     subscription", text)
        self.assertEqual(self.decisions(fix), [])

    def test_an_unapproved_task_blocks(self):
        fix = Fixture(self.dirs, task=TASK.replace("state: approved", "state: draft"))
        code, text = self.run_main(fix=fix)
        self.assertEqual(code, launch.EXIT_BLOCK)
        self.assertIn("BLOCKED. Nothing was run.", text)

    def test_an_unpriced_task_asks_and_says_why(self):
        fix = Fixture(self.dirs, task=TASK.replace("est_p95_usd: 6.00\n", ""))
        code, text = self.run_main(fix=fix)
        self.assertEqual(code, launch.EXIT_ASK)
        self.assertIn("cannot estimate", text)
        self.assertIn("ledger/run-tasks.json", text)

    def test_a_task_over_threshold_asks(self):
        fix = Fixture(self.dirs, task=TASK.replace("est_p95_usd: 6.00", "est_p95_usd: 16.00"))
        code, text = self.run_main(fix=fix)
        self.assertEqual(code, launch.EXIT_ASK)
        self.assertIn("above the $10.00 threshold", text)

    def test_a_broken_gate_config_is_an_error_not_a_default_threshold(self):
        self.fix.config.write_text("{ not json")
        code, text = self.run_main()
        self.assertEqual(code, launch.EXIT_ERROR)
        self.assertIn("ERROR", text)

    def test_missing_permission_mode_is_refused(self):
        config = dict(GATE_CONFIG, defaults={"allowed_tools": ["Read"],
                                             "output_format": "json"})
        fix = Fixture(self.dirs, config=config)
        code, text = self.run_main(fix=fix)
        self.assertEqual(code, launch.EXIT_ERROR)
        self.assertIn("starts in Manual", text)

    def test_a_missing_task_file_is_an_error(self):
        out = io.StringIO()
        code = launch.main([str(self.fix.dir / "nope.md"),
                            "--gate-config", str(self.fix.config)], env={}, out=out)
        self.assertEqual(code, launch.EXIT_ERROR)
        self.assertIn("No task file", out.getvalue())


if __name__ == "__main__":
    unittest.main()
