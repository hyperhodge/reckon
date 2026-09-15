# SPDX-License-Identifier: Apache-2.0
"""Tests for reckon/cli.py: each subcommand reaches the right module with the
right arguments (RECKON-1.1-SPEC.md R1's acceptance test), reckon --help
lists all four subcommands, and the install check gates every one of them."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reckon import cli, install_check  # noqa: E402


class TestHelpListsFourSubcommands(unittest.TestCase):
    def test_help_names_estimate_gate_record_and_report(self):
        text = cli.build_parser().format_help()
        for name in ("estimate", "gate", "record", "report"):
            self.assertIn(name, text)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(install_check, "require_linked_install",
                                    return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_estimate_passes_a_positional_proposal_through(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        with mock.patch.object(cli, "_module", return_value=fake) as module_lookup:
            rc = cli.main(["estimate", "Build a thing", "--json"])
        self.assertEqual(rc, 0)
        module_lookup.assert_called_once_with("estimate", "estimator")
        fake.main.assert_called_once_with(["--proposal", "Build a thing", "--json"])

    def test_estimate_file_is_used_instead_of_a_positional_proposal(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        with mock.patch.object(cli, "_module", return_value=fake):
            cli.main(["estimate", "--file", "p.txt"])
        fake.main.assert_called_once_with(["--file", "p.txt"])

    def test_gate_passes_the_task_file_and_execute_through_unchanged(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        with mock.patch.object(cli, "_module", return_value=fake) as module_lookup:
            rc = cli.main(["gate", "tasks/P-0001-T61.md", "--execute"])
        self.assertEqual(rc, 0)
        module_lookup.assert_called_once_with("launch", "wrapper")
        fake.main.assert_called_once_with(["tasks/P-0001-T61.md", "--execute"])

    def test_gate_defaults_to_dry_run_by_passing_no_execute_flag(self):
        fake = mock.Mock()
        fake.main.return_value = 0
        with mock.patch.object(cli, "_module", return_value=fake):
            cli.main(["gate", "tasks/P-0001-T61.md"])
        fake.main.assert_called_once_with(["tasks/P-0001-T61.md"])

    def test_record_calls_the_record_module_with_source_and_session(self):
        with mock.patch.object(cli.record_mod, "run", return_value=0) as record_run:
            rc = cli.main(["record", "--source", "/tmp/x", "--session", "abc"])
        self.assertEqual(rc, 0)
        record_run.assert_called_once_with(source="/tmp/x", session="abc")

    def test_report_calls_the_report_module_with_sample(self):
        with mock.patch.object(cli.report_mod, "run", return_value=0) as report_run:
            rc = cli.main(["report", "--sample"])
        self.assertEqual(rc, 0)
        report_run.assert_called_once_with(sample=True)

    def test_no_command_prints_help_and_returns_nonzero(self):
        rc = cli.main([])
        self.assertEqual(rc, 1)


class TestInstallCheckGatesEveryCommand(unittest.TestCase):
    def test_a_refused_install_stops_before_any_module_runs(self):
        with mock.patch.object(install_check, "require_linked_install",
                               side_effect=install_check.NotLinkedInstall("nope")):
            with mock.patch.object(cli, "_module") as module_lookup:
                rc = cli.main(["estimate", "text"])
        self.assertEqual(rc, 2)
        module_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
