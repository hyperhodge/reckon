#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s ledger/tests

Plain unittest, no third-party dependencies. Every fixture is written to a
temporary directory; nothing here reads or writes ~/.claude.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aggregate  # noqa: E402


PRICES = {
    "source": "test fixture",
    "checked_on": "2026-09-04",
    "models": {
        # 1 / 10 / 100 / 1000 per million: deliberately unlike anything real, so
        # a test that passes because it accidentally used the shipped table fails.
        "claude-test-1": {"input": 1.0, "output": 10.0,
                          "cache_write_5m": 100.0, "cache_write_1h": None,
                          "cache_read": 1000.0},
        "claude-opus-5": {"input": 5.0, "output": 25.0,
                          "cache_write_5m": 6.25, "cache_write_1h": None,
                          "cache_read": 0.50},
    },
    "ignored_models": ["<synthetic>"],
}

RAILS = {"default": "subscription", "rules": []}


def assistant_line(msg_id, model="claude-test-1", date="2026-09-01",
                   inp=0, out=0, write_5m=0, write_1h=0, read=0,
                   cwd="/Users/x/Projects/demo", entrypoint="claude-desktop"):
    return json.dumps({
        "type": "assistant",
        "timestamp": f"{date}T10:00:00.000Z",
        "cwd": cwd,
        "entrypoint": entrypoint,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": write_5m + write_1h,
                "cache_read_input_tokens": read,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": write_5m,
                    "ephemeral_1h_input_tokens": write_1h,
                },
            },
        },
    })


class LedgerTestCase(unittest.TestCase):
    """Writes a fake transcript tree and a price table into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects" / "-Users-x-Projects-demo"
        self.projects.mkdir(parents=True)
        self.prices_path = self.root / "prices.json"
        self.prices_path.write_text(json.dumps(PRICES), encoding="utf-8")
        self.rails_path = self.root / "rails.json"
        self.rails_path.write_text(json.dumps(RAILS), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def write_transcript(self, lines, name="session.jsonl"):
        (self.projects / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_collect(self):
        prices = aggregate.load_prices(self.prices_path)
        rails = aggregate.load_rails(self.rails_path)
        records = aggregate.iter_transcripts(self.projects.parent)
        return aggregate.collect(records, prices, rails)


class TestDedupe(LedgerTestCase):
    """Claude Code writes one line per content block and repeats the whole
    usage object on each. Counting a message twice inflates every downstream
    figure, so this is the one that must not regress."""

    def test_repeated_message_id_counted_once(self):
        line = assistant_line("msg_A", out=1000)
        self.write_transcript([line, line, line])
        buckets, _unpriced, stats = self.run_collect()

        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["duplicate_lines"], 2)
        (bucket,) = buckets.values()
        self.assertEqual(bucket["output"], 1000)

    def test_dedupe_spans_files(self):
        """A resumed session repeats messages in a second file."""
        line = assistant_line("msg_A", out=1000)
        self.write_transcript([line], name="one.jsonl")
        self.write_transcript([line], name="two.jsonl")
        buckets, _unpriced, stats = self.run_collect()

        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(sum(b["output"] for b in buckets.values()), 1000)

    def test_distinct_ids_both_counted(self):
        self.write_transcript([assistant_line("msg_A", out=10),
                               assistant_line("msg_B", out=32)])
        buckets, _unpriced, stats = self.run_collect()

        self.assertEqual(stats["assistant_messages"], 2)
        self.assertEqual(sum(b["output"] for b in buckets.values()), 42)


class TestPricing(unittest.TestCase):
    """Arithmetic on one known row, checked against a hand calculation."""

    def test_known_row(self):
        price = PRICES["models"]["claude-test-1"]
        counts = {"input": 1_000_000, "output": 1_000_000,
                  "cache_write_5m": 1_000_000, "cache_write_1h": 0,
                  "cache_read": 1_000_000}
        cost = aggregate.message_cost(counts, price)

        # One million of each, at 1 / 10 / 100 / 1000 per million.
        self.assertAlmostEqual(cost["input"], 1.0)
        self.assertAlmostEqual(cost["output"], 10.0)
        self.assertAlmostEqual(cost["cache_write"], 100.0)
        self.assertAlmostEqual(cost["cache_read"], 1000.0)
        self.assertAlmostEqual(sum(cost.values()), 1111.0)

    def test_realistic_opus_row(self):
        price = PRICES["models"]["claude-opus-5"]
        counts = {"input": 2, "output": 12, "cache_write_5m": 0,
                  "cache_write_1h": 24_288, "cache_read": 39_088}
        cost = aggregate.message_cost(counts, price)

        self.assertAlmostEqual(cost["input"], 2 * 5.0 / 1e6)
        self.assertAlmostEqual(cost["output"], 12 * 25.0 / 1e6)
        # No 1h rate in the table, so 1h writes fall back to the 5m rate.
        self.assertAlmostEqual(cost["cache_write"], 24_288 * 6.25 / 1e6)
        self.assertAlmostEqual(cost["cache_read"], 39_088 * 0.50 / 1e6)

    def test_1h_rate_used_when_present(self):
        price = dict(PRICES["models"]["claude-opus-5"], cache_write_1h=10.0)
        counts = {"input": 0, "output": 0, "cache_write_5m": 1_000_000,
                  "cache_write_1h": 1_000_000, "cache_read": 0}
        cost = aggregate.message_cost(counts, price)

        self.assertAlmostEqual(cost["cache_write"], 6.25 + 10.0)

    def test_date_suffix_stripped(self):
        self.assertIsNotNone(aggregate.price_for("claude-opus-5-20250929", PRICES))

    def test_missing_price_table_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "prices.json"
            with self.assertRaises(aggregate.PriceTableError) as ctx:
                aggregate.load_prices(missing)
            self.assertIn("Without it every row would cost $0.00", str(ctx.exception))


class TestUnpriced(LedgerTestCase):
    def test_unpriced_model_contributes_zero_and_is_reported(self):
        self.write_transcript([
            assistant_line("msg_A", model="claude-test-1", out=1_000_000),
            assistant_line("msg_B", model="claude-unknown-9", out=1_000_000),
        ])
        buckets, unpriced, _stats = self.run_collect()

        self.assertEqual(unpriced, ["claude-unknown-9"])
        costs = {model: sum(b["cost"].values())
                 for (_d, _p, model), b in buckets.items()}
        self.assertAlmostEqual(costs["claude-test-1"], 10.0)
        self.assertEqual(costs["claude-unknown-9"], 0.0)   # zero, never a guess

    def test_ignored_model_is_not_reported_as_unpriced(self):
        self.write_transcript([assistant_line("msg_A", model="<synthetic>")])
        _buckets, unpriced, stats = self.run_collect()

        self.assertEqual(unpriced, [])
        self.assertEqual(stats["assistant_messages"], 0)


class TestCacheReadRatio(LedgerTestCase):
    def test_ratio_maths(self):
        # 2M cache reads at 1000/M = $2000; 100M output... keep it simple:
        # 1M cache read = $1000, 50M output-equivalent is unwieldy, so use
        # 1M read ($1000) against 20M output ($200) -> ratio 5.0.
        self.write_transcript([
            assistant_line("msg_A", read=1_000_000, out=20_000_000),
        ])
        buckets, _unpriced, _stats = self.run_collect()
        analysis = aggregate.build_analysis(buckets)
        overall = analysis["overall"]

        self.assertAlmostEqual(overall["cache_read_cost"], 1000.0)
        self.assertAlmostEqual(overall["output_cost"], 200.0)
        self.assertAlmostEqual(overall["cache_read_to_output_ratio"], 5.0)
        # Input side here is cache read only, so it is 100% of the input side.
        self.assertAlmostEqual(overall["cache_read_share_of_input_side"], 1.0)
        self.assertAlmostEqual(overall["input_side_share_of_total"], 1000 / 1200, places=4)

    def test_zero_output_gives_none_not_division_error(self):
        self.write_transcript([assistant_line("msg_A", read=1_000_000, out=0)])
        buckets, _unpriced, _stats = self.run_collect()
        overall = aggregate.build_analysis(buckets)["overall"]

        self.assertIsNone(overall["cache_read_to_output_ratio"])

    def test_per_model_and_per_project_slices_exist(self):
        self.write_transcript([
            assistant_line("msg_A", read=1_000_000, out=1_000_000,
                           cwd="/Users/x/Projects/alpha"),
            assistant_line("msg_B", model="claude-opus-5", read=1_000_000,
                           out=1_000_000, cwd="/Users/x/Projects/beta"),
        ])
        buckets, _unpriced, _stats = self.run_collect()
        analysis = aggregate.build_analysis(buckets)

        self.assertEqual(set(analysis["by_model"]), {"claude-test-1", "claude-opus-5"})
        self.assertEqual(set(analysis["by_project"]), {"alpha", "beta"})


class TestSyntheticQuarantine(unittest.TestCase):
    """Real and fabricated cost must never share a total."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        projects = self.root / "projects" / "-Users-x-Projects-demo"
        projects.mkdir(parents=True)
        # A model that is in the shipped prices.json, so real cost is non-zero
        # and a total that silently absorbed the fabricated rows would show.
        (projects / "s.jsonl").write_text(
            assistant_line("msg_A", model="claude-opus-5",
                           out=1_000_000, read=4_000_000) + "\n", encoding="utf-8")
        self.projects_dir = projects.parent
        self.out = self.root / "usage.json"
        # main() reads the shipped prices.json/rails.json, which is what we want
        # to exercise here -- only the transcript root and output are redirected.

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, extra_args=()):
        """main() prints its summary; swallow it so test output stays readable."""
        with contextlib.redirect_stdout(io.StringIO()):
            rc = aggregate.main(["--projects", str(self.projects_dir),
                                 "--out", str(self.out), *extra_args])
        self.assertEqual(rc, 0)
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_default_run_has_no_synthetic_rows(self):
        payload = self._run()

        self.assertEqual([r for r in payload["days"] if r["synthetic"]], [])
        self.assertFalse(payload["totals"]["illustrative"]["included"])
        self.assertEqual(payload["totals"]["illustrative"]["cost_usd"], 0.0)

    def test_flag_adds_rows_but_keeps_totals_apart(self):
        payload = self._run(["--with-illustrative"])
        synthetic = [r for r in payload["days"] if r["synthetic"]]
        real = [r for r in payload["days"] if not r["synthetic"]]

        self.assertTrue(synthetic)
        self.assertTrue(all(r["cost"] > 0 for r in synthetic))
        self.assertTrue(payload["totals"]["illustrative"]["included"])

        real_total = payload["totals"]["real"]["cost_usd"]
        illustrative_total = payload["totals"]["illustrative"]["cost_usd"]
        self.assertGreater(real_total, 0.0)
        self.assertAlmostEqual(real_total, sum(r["cost"] for r in real), places=6)
        self.assertAlmostEqual(illustrative_total,
                               sum(r["cost"] for r in synthetic), places=4)
        # The two must not have been folded together anywhere.
        self.assertNotAlmostEqual(real_total, real_total + illustrative_total)
        self.assertNotIn("cost_usd", payload["totals"].keys())

    def test_analysis_ignores_illustrative_rows(self):
        """Fabricated rows carry no tokens, so they must not move the answer."""
        plain = self._run()["analysis"]["overall"]
        with_illustrative = self._run(["--with-illustrative"])["analysis"]["overall"]

        self.assertEqual(plain, with_illustrative)


class TestRails(LedgerTestCase):
    def test_default_rail_is_counted(self):
        self.write_transcript([assistant_line("msg_A", out=10)])
        buckets, _unpriced, _stats = self.run_collect()
        rows, defaulted = aggregate.buckets_to_rows(buckets, RAILS)

        self.assertEqual(defaulted, 1)
        self.assertEqual(rows[0]["rail"], "subscription")
        self.assertTrue(rows[0]["rail_assumed"])

    def test_rule_overrides_by_project_and_date(self):
        rails = {"default": "subscription",
                 "rules": [{"project": "demo", "from": "2026-09-01",
                            "to": "2026-09-30", "rail": "api"}]}
        self.write_transcript([assistant_line("msg_A", date="2026-09-15", out=10),
                               assistant_line("msg_B", date="2026-08-15", out=10)])
        buckets, _unpriced, _stats = self.run_collect()
        rows, defaulted = aggregate.buckets_to_rows(buckets, rails)
        by_date = {r["date"]: r for r in rows}

        self.assertEqual(by_date["2026-09-15"]["rail"], "api")
        self.assertFalse(by_date["2026-09-15"]["rail_assumed"])
        self.assertEqual(by_date["2026-08-15"]["rail"], "subscription")
        self.assertEqual(defaulted, 1)


class TestEntrypoint(LedgerTestCase):
    """How a session was launched is the only rail signal the transcript
    carries, so it is recorded rather than assumed away."""

    def test_entrypoint_recorded_on_the_row(self):
        self.write_transcript([assistant_line("msg_A", out=10)])
        buckets, _unpriced, stats = self.run_collect()
        rows, _defaulted = aggregate.buckets_to_rows(buckets, RAILS)

        self.assertEqual(rows[0]["entrypoints"], ["claude-desktop"])
        self.assertEqual(stats["entrypoints"], {"claude-desktop": 1})

    def test_mixed_entrypoints_both_survive(self):
        """A scripted run alongside desktop ones must not be averaged away."""
        self.write_transcript([
            assistant_line("msg_A", out=10),
            assistant_line("msg_B", out=10, entrypoint="cli"),
        ])
        buckets, _unpriced, stats = self.run_collect()
        rows, _defaulted = aggregate.buckets_to_rows(buckets, RAILS)

        self.assertEqual(rows[0]["entrypoints"], ["claude-desktop", "cli"])
        self.assertEqual(stats["entrypoints"], {"claude-desktop": 1, "cli": 1})


class TestParseErrors(LedgerTestCase):
    def test_bad_line_is_skipped_and_counted(self):
        self.write_transcript([assistant_line("msg_A", out=10),
                               "{not json at all",
                               assistant_line("msg_B", out=10)])
        _buckets, _unpriced, stats = self.run_collect()

        self.assertEqual(stats["parse_errors"], 1)
        self.assertEqual(stats["assistant_messages"], 2)   # run did not abort


if __name__ == "__main__":
    unittest.main(verbosity=2)
