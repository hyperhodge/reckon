# SPDX-License-Identifier: Apache-2.0
"""Tests for the estimator (P-0001-T45).

The tests that matter here are not the arithmetic ones. They are the ones that fail if the
estimator ever becomes the thing it was built not to be: a confident point number.
"""

import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import estimate as E  # noqa: E402


class TestFeatures(unittest.TestCase):
    def test_splits_a_multi_part_proposal(self):
        items = E.split_items("Build the reader; wire it to the screen, and write the notes")
        self.assertEqual(len(items), 3)

    def test_a_single_sentence_is_one_item(self):
        self.assertEqual(len(E.split_items("Put the weekly limit on a screen")), 1)

    def test_empty_proposal_does_not_explode(self):
        self.assertEqual(E.split_items(""), [""])

    def test_an_explicit_count_is_read_from_the_words(self):
        self.assertEqual(E.counted_multiplicity("the 16 acceptance queries"), 16)
        self.assertEqual(E.counted_multiplicity("four items"), 4)
        self.assertEqual(E.counted_multiplicity("one screen"), 1)

    def test_a_count_is_capped_so_one_phrase_cannot_dominate(self):
        self.assertEqual(E.counted_multiplicity("all 97 records"), 16)


class TestModelShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = E.load_model()

    def test_the_model_on_disk_carries_its_own_backtest(self):
        self.assertIn("backtest", self.model)
        self.assertIsNotNone(self.model["backtest"]["hit_rate"])

    def test_the_selected_feature_set_beat_the_alternatives_out_of_sample(self):
        tm = self.model["turn_model"]
        chosen = tm["candidates_loo_rmse_log"][tm["selected"]]
        for name, rmse in tm["candidates_loo_rmse_log"].items():
            self.assertLessEqual(chosen, rmse, f"{name} predicts better than the selected model")

    def test_the_honest_finding_is_recorded_and_not_quietly_dropped(self):
        # If someone ever finds real signal in the words this assertion should fail loudly,
        # because the note beside it will then be wrong and must be rewritten.
        self.assertLess(self.model["turn_model"]["improvement_over_median_only"], 0.2)
        self.assertIn("BARELY PREDICT", self.model["turn_model"]["_what_the_selection_means"])

    def test_the_realisation_factor_is_derived_from_two_measured_ratios(self):
        r = self.model["realisation"]
        self.assertAlmostEqual(r["combined"],
                               r["bottom_up_to_direct"] * r["direct_to_realised"], places=6)
        self.assertGreater(r["combined"], 1.0)


class TestEstimate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = E.load_model()

    def test_never_returns_a_point_estimate(self):
        est = E.estimate("Put the weekly rate limit on a screen", self.model)
        self.assertLess(est["cost_usd"]["low"], est["cost_usd"]["p50"])
        self.assertLess(est["cost_usd"]["p50"], est["cost_usd"]["high"])

    def test_every_estimate_carries_a_hit_rate(self):
        est = E.estimate("Anything at all", self.model)
        self.assertIsNotNone(est["hit_rate"])

    def test_every_estimate_carries_the_floor_rule_so_it_cannot_be_read_as_a_bill(self):
        est = E.estimate("Anything at all", self.model)
        self.assertIn("floor", est["floor_rule"].lower())
        self.assertIn("not a bill", est["floor_rule"].lower())

    def test_a_bigger_proposal_costs_more_than_a_smaller_one(self):
        small = E.estimate("Put the weekly limit on a screen", self.model)
        big = E.estimate("Put the weekly limit on a screen; rebuild the store; "
                         "rewrite the queries, and document all of it", self.model)
        self.assertGreater(big["cost_usd"]["p50"], small["cost_usd"]["p50"])

    def test_the_breakdown_adds_up_to_the_total_it_is_shown_beside(self):
        est = E.estimate("Build a thing; test the thing, and write it up", self.model)
        self.assertAlmostEqual(sum(i["cost_usd_p50"] for i in est["items"]),
                               est["cost_usd"]["p50"], places=6)

    def test_the_realisation_factor_is_applied_unless_it_would_be_circular(self):
        raw = E.estimate("Build a thing", self.model, apply_realisation=False)
        cooked = E.estimate("Build a thing", self.model)
        self.assertGreater(cooked["cost_usd"]["p50"], raw["cost_usd"]["p50"])


class TestCeilingAndSessions(unittest.TestCase):
    def test_work_past_the_ceiling_is_priced_as_several_sessions(self):
        self.assertEqual(E.sessions_for_turns(10), 1)
        cap = E.ceiling()["messages"]
        self.assertEqual(E.sessions_for_turns(cap * 2 + 1), 3)

    def test_the_ceiling_is_read_from_the_gate_and_not_duplicated_here(self):
        gate = json.loads((E.ROOT / "wrapper" / "gate.json").read_text())
        self.assertEqual(E.ceiling()["messages"], gate["session_ceiling_assistant_messages"])
        self.assertEqual(E.ceiling()["gbp"], gate["session_ceiling_gbp"])

    def test_splitting_at_the_ceiling_beats_one_long_session(self):
        # The whole argument for handing off. If this ever inverts, the curve has changed and
        # the ceiling's own basis needs re-deriving.
        model = E.load_model()
        cap = E.ceiling()["messages"]
        one_long = (model["cost_curve"]["a_per_message"] * (cap * 2)
                    + model["cost_curve"]["b_per_message_squared_over_100"]
                    * (cap * 2) ** 2 / 100.0)
        self.assertLess(E.cost_for_turns(cap * 2, model, "0.5"),
                        one_long * model["cost_curve"]["ratio_quantiles"]["0.5"])


class TestAggregate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = E.load_model()

    def test_a_programme_total_is_tighter_than_one_item_only_if_errors_are_independent(self):
        proposals = [{"id": f"X-{i}", "text": "Build a thing and test it"} for i in range(16)]
        agg = E.aggregate(proposals, self.model)
        independent_width = agg["independent"]["high"] / agg["independent"]["low"]
        systematic_width = agg["systematic"]["high"] / agg["systematic"]["low"]
        self.assertLess(independent_width, systematic_width)

    def test_both_ends_are_always_reported_so_neither_can_be_quoted_alone(self):
        agg = E.aggregate([{"id": "X", "text": "Build a thing"}], self.model)
        self.assertIn("independent", agg)
        self.assertIn("systematic", agg)
        self.assertIn("top_down_cross_check_usd", agg)

    def test_session_count_uses_the_realised_turns_not_the_raw_ones(self):
        proposals = [{"id": f"X-{i}", "text": "Build a thing"} for i in range(20)]
        agg = E.aggregate(proposals, self.model)
        self.assertGreater(agg["total_turns_realised_p50"], agg["total_turns_p50"])
        self.assertEqual(agg["sessions_at_ceiling"],
                         math.ceil(agg["total_turns_realised_p50"] / E.ceiling()["messages"]))


class TestBacktest(unittest.TestCase):
    def test_the_backtest_is_leave_one_out_and_says_so(self):
        model = E.load_model()
        bt = model["backtest"]
        self.assertIn("leave-one-out", bt["method"])
        self.assertEqual(bt["n"], len(model["turn_samples"]))

    def test_the_hand_estimate_baseline_is_recorded_beside_it(self):
        model = E.load_model()
        self.assertIn("baseline_hand_estimates", model)
        self.assertIsNotNone(model["baseline_hand_estimates"]["hit_rate"])

if __name__ == "__main__":
    unittest.main()
