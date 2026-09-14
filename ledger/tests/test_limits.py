#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests for limits.py. The thing worth protecting is honesty: an observed
ceiling is a marker, and the tool must not present it as a gauge."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import limits as L  # noqa: E402

UTC = timezone.utc


class TestWindowStart(unittest.TestCase):
    """A rolling window restarts at a reset; consumption before it stops counting."""

    def setUp(self):
        self.end = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
        self.window = timedelta(hours=5)

    def test_no_resets_uses_naive_start(self):
        self.assertEqual(L.window_start(self.end, self.window, []),
                         self.end - self.window)

    def test_reset_inside_the_window_wins(self):
        reset = datetime(2026, 9, 5, 1, 0, tzinfo=UTC)   # later than 23:00
        self.assertEqual(L.window_start(self.end, self.window, [reset]), reset)

    def test_reset_older_than_the_window_is_ignored(self):
        reset = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)  # long before 23:00
        self.assertEqual(L.window_start(self.end, self.window, [reset]),
                         self.end - self.window)

    def test_future_reset_is_ignored(self):
        reset = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)   # has not happened yet
        self.assertEqual(L.window_start(self.end, self.window, [reset]),
                         self.end - self.window)

    def test_most_recent_passed_reset_wins(self):
        resets = [datetime(2026, 9, 5, 0, 30, tzinfo=UTC),
                  datetime(2026, 9, 5, 2, 0, tzinfo=UTC)]
        self.assertEqual(L.window_start(self.end, self.window, resets), resets[1])


def counts(**kw):
    base = {"input": 0, "output": 0, "cache_write_5m": 0,
            "cache_write_1h": 0, "cache_read": 0}
    base.update(kw)
    return base


class TestConsumption(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
        self.msgs = [
            (self.end - timedelta(hours=9), counts(output=1000), 1.0),   # outside
            (self.end - timedelta(hours=2), counts(output=10), 0.5),     # inside
            (self.end - timedelta(minutes=5), counts(output=32, cache_read=7), 0.25),
        ]

    def test_only_messages_in_the_window_count(self):
        got = L.consumption(self.msgs, self.end, timedelta(hours=5))
        self.assertEqual(got["output"], 42)
        self.assertEqual(got["messages"], 2)
        self.assertAlmostEqual(got["cost"], 0.75)

    def test_all_classes_sums_every_class(self):
        got = L.consumption(self.msgs, self.end, timedelta(hours=5))
        self.assertEqual(got["all_classes"], 42 + 7)

    def test_a_reset_excludes_earlier_consumption(self):
        reset = self.end - timedelta(hours=1)
        got = L.consumption(self.msgs, self.end, timedelta(hours=5), [reset])
        self.assertEqual(got["output"], 32)      # the 2-hours-ago message is gone
        self.assertEqual(got["messages"], 1)


class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 9, 4, 21, 26, tzinfo=UTC)
        self.msgs = [(self.at - timedelta(minutes=30), counts(output=100, cache_read=900), 1.0)]
        self.events = [{
            "at": self.at.isoformat(), "type": "five_hour", "status": "rejected",
            "overage_status": "rejected", "overage_disabled_reason": "org_level_disabled",
            "using_overage": False, "resets_at": None, "project": "demo",
        }]

    def test_a_refusal_becomes_a_calibration_point(self):
        points = L.calibrate(self.msgs, self.events)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["consumed"]["all_classes"], 1000)
        self.assertTrue(points[0]["overage_would_have_charged"])

    def test_a_non_rejection_is_not_a_calibration_point(self):
        self.events[0]["status"] = "allowed"
        self.assertEqual(L.calibrate(self.msgs, self.events), [])

    def test_single_observation_is_labelled_as_a_marker(self):
        observed = L.ceilings(L.calibrate(self.msgs, self.events))
        self.assertEqual(observed["five_hour"]["observations"], 1)
        self.assertIn("not a denominator", observed["five_hour"]["basis"])

    def test_lowest_refusal_is_the_working_ceiling(self):
        second = dict(self.events[0])
        second["at"] = (self.at + timedelta(days=1)).isoformat()
        msgs = self.msgs + [(self.at + timedelta(days=1) - timedelta(minutes=5),
                             counts(output=50), 0.1)]
        observed = L.ceilings(L.calibrate(msgs, [self.events[0], second]))

        self.assertEqual(observed["five_hour"]["observations"], 2)
        self.assertEqual(observed["five_hour"]["lowest_refusal_tokens"], 50)
        self.assertIn("lowest", observed["five_hour"]["basis"])

    def test_no_refusals_means_no_ceiling_claimed(self):
        self.assertEqual(L.ceilings([]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
