#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 -m unittest discover -s wrapper/tests

Plain unittest, no third-party dependencies. Nothing here reads the real
gate.json, ~/.claude, or the network: every fixture is built in the test.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate  # noqa: E402


# Threshold deliberately unlike the real $10 in the tests that do not care
# about the boundary, so a test that passes by accidentally loading the shipped
# config fails instead.
def config(threshold=10.0, minimum=3, priors=None, ceiling=5.0):
    return {
        "threshold_usd": threshold,
        "min_samples_for_prior": minimum,
        "api_rail_cap_ceiling_usd": ceiling,
        "priors": priors if priors is not None else {},
        "defaults": {"permission_mode": "acceptEdits",
                     "allowed_tools": ["Read", "Edit"],
                     "output_format": "json"},
    }


def approved(**extra):
    front = {"ref": "P-0001-T03", "state": "approved", "approved_by": "ada"}
    front.update(extra)
    return front


class LoadConfig(unittest.TestCase):
    def test_missing_file_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gate.GateConfigError):
                gate.load_config(Path(tmp) / "nope.json")

    def test_no_threshold_is_fatal_rather_than_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gate.json"
            path.write_text(json.dumps({"min_samples_for_prior": 3}))
            with self.assertRaises(gate.GateConfigError):
                gate.load_config(path)

    def test_the_shipped_config_loads_and_says_ten_dollars(self):
        loaded = gate.load_config()
        self.assertEqual(loaded["threshold_usd"], 10.0)
        self.assertEqual(loaded["min_samples_for_prior"], 3)


class EstimatePaths(unittest.TestCase):
    """The three paths, in the order settled on 5 September."""

    def test_1_front_matter_wins(self):
        priors = {"code-task": {"n": 3, "samples_usd": [1.0, 2.0, 99.0]}}
        est = gate.estimate_cost(approved(est_p95_usd="6.00", task_class="code-task"),
                                 config(priors=priors))
        self.assertEqual(est.source, gate.SOURCE_FRONT_MATTER)
        self.assertEqual(est.usd, 6.00)

    def test_2_prior_used_when_it_has_enough_samples(self):
        priors = {"code-task": {"n": 3, "samples_usd": [3.72, 3.68, 4.10]}}
        est = gate.estimate_cost(approved(task_class="code-task"), config(priors=priors))
        self.assertEqual(est.source, gate.SOURCE_PRIOR)
        # p95 with three samples is the worst observed, not a fitted quantile.
        self.assertEqual(est.usd, 4.10)

    def test_2_prior_refused_below_min_samples(self):
        priors = {"code-task": {"n": 2, "samples_usd": [3.72, 3.68]}}
        est = gate.estimate_cost(approved(task_class="code-task"), config(priors=priors))
        self.assertIsNone(est.usd)
        self.assertIn("n=2", est.basis)
        self.assertIn("min_samples_for_prior=3", est.basis)

    def test_3_no_estimate_at_all(self):
        est = gate.estimate_cost(approved(), config())
        self.assertIsNone(est.usd)
        self.assertEqual(est.source, gate.SOURCE_NONE)

    def test_unknown_class_names_the_classes_it_does_know(self):
        priors = {"html-artifact": {"n": 3, "samples_usd": [1.0, 2.0, 3.0]}}
        est = gate.estimate_cost(approved(task_class="cv"), config(priors=priors))
        self.assertIsNone(est.usd)
        self.assertIn("html-artifact", est.basis)

    def test_declared_n_disagreeing_with_samples_is_refused_not_averaged(self):
        priors = {"code-task": {"n": 5, "samples_usd": [3.0, 4.0, 5.0]}}
        est = gate.estimate_cost(approved(task_class="code-task"), config(priors=priors))
        self.assertIsNone(est.usd)
        self.assertIn("declares n=5", est.basis)

    def test_malformed_front_matter_estimate_does_not_fall_through_to_a_prior(self):
        priors = {"code-task": {"n": 3, "samples_usd": [1.0, 1.0, 1.0]}}
        est = gate.estimate_cost(
            approved(est_p95_usd="six dollars", task_class="code-task"),
            config(priors=priors))
        self.assertIsNone(est.usd)
        self.assertIn("not a positive number", est.basis)

    def test_negative_estimate_is_not_an_estimate(self):
        est = gate.estimate_cost(approved(est_p95_usd="-4"), config())
        self.assertIsNone(est.usd)

    def test_task_type_is_accepted_as_the_class_when_task_class_is_absent(self):
        priors = {"execute": {"n": 3, "samples_usd": [2.0, 2.5, 3.0]}}
        est = gate.estimate_cost(approved(task_type="execute"), config(priors=priors))
        self.assertEqual(est.usd, 3.0)

    def test_at_least_one_shipped_prior_is_now_usable(self):
        """The predecessor of this test asserted that every shipped prior was
        still too thin to use, and existed so that it would FAIL the day the
        Recorder landed. It failed on 5 September 2026. Estimate path 2 is now
        live, and this pins it open: if every prior goes back below
        min_samples_for_prior, the gate has quietly lost its second estimate
        path and is one front-matter typo away from asking about everything."""
        loaded = gate.load_config()
        usable = [name for name in loaded["priors"]
                  if gate.prior_for(name, loaded)[0] is not None]
        self.assertTrue(usable, "no prior in gate.json is usable any more; "
                                "re-derive with ledger/recorder.py --priors")

    def test_every_derived_prior_names_the_runs_it_came_from(self):
        """A prior is a number the gate acts on. One with no basis is a number
        someone typed, and the whole point of deriving them is that nobody did."""
        loaded = gate.load_config()
        for name, prior in loaded["priors"].items():
            self.assertTrue((prior.get("basis") or "").strip(),
                            f"prior '{name}' carries no basis")
            self.assertEqual(len(prior.get("samples_usd") or []), prior.get("n"),
                             f"prior '{name}' declares n={prior.get('n')} but "
                             f"carries a different number of samples")


class Boundary(unittest.TestCase):
    """The threshold is the point ABOVE which a run re-gates, so exactly $10
    allows. gate.json's threshold_basis says so; this pins it."""

    def decide_at(self, usd, **kw):
        return gate.decide(approved(est_p95_usd=str(usd)), config(), **kw)

    def test_just_under_allows(self):
        self.assertEqual(self.decide_at("9.99").action, gate.ALLOW)

    def test_exactly_ten_allows(self):
        self.assertEqual(self.decide_at("10.00").action, gate.ALLOW)

    def test_just_over_asks(self):
        decision = self.decide_at("10.01")
        self.assertEqual(decision.action, gate.ASK)
        self.assertIn("above the $10.00 threshold", decision.reasons[0])

    def test_float_noise_at_the_boundary_still_allows(self):
        # 10.00 arriving as 9.999999999 or 10.000000001 must not flip the gate.
        self.assertEqual(self.decide_at("9.999999999").action, gate.ALLOW)
        self.assertEqual(self.decide_at("10.000000001").action, gate.ALLOW)

    def test_the_five_september_front_end_build_would_have_asked(self):
        # ~$16, the run the threshold was chosen against.
        self.assertEqual(self.decide_at("16.00").action, gate.ASK)

    def test_an_ordinary_task_run_allows(self):
        self.assertEqual(self.decide_at("3.72").action, gate.ALLOW)


class Decisions(unittest.TestCase):
    def test_unapproved_task_is_blocked_not_asked(self):
        front = approved(est_p95_usd="1.00")
        front["state"] = "draft"
        decision = gate.decide(front, config())
        self.assertEqual(decision.action, gate.BLOCK)
        self.assertIn("not 'approved'", decision.reasons[0])

    def test_missing_state_is_blocked(self):
        decision = gate.decide({"ref": "X", "est_p95_usd": "1.00"}, config())
        self.assertEqual(decision.action, gate.BLOCK)
        self.assertIn("unset", decision.reasons[0])

    def test_no_estimate_asks_and_says_why_run_level_cost_is_missing(self):
        decision = gate.decide(approved(), config())
        self.assertEqual(decision.action, gate.ASK)
        self.assertIn(gate.NO_RUN_LEVEL_COST_NOTE, decision.reasons)

    def test_there_is_no_silent_pass(self):
        for front in (approved(), approved(est_p95_usd="1"), approved(est_p95_usd="99"),
                      {"ref": "X"}):
            action = gate.decide(front, config()).action
            self.assertIn(action, (gate.ALLOW, gate.ASK, gate.BLOCK))

    def test_scheduled_run_under_threshold_does_not_re_gate(self):
        decision = gate.decide(approved(est_p95_usd="4.00"), config(), scheduled=True)
        self.assertEqual(decision.action, gate.ALLOW)

    def test_scheduled_run_over_threshold_re_gates_and_says_that_is_the_exception(self):
        decision = gate.decide(approved(est_p95_usd="12.00"), config(), scheduled=True)
        self.assertEqual(decision.action, gate.ASK)
        self.assertTrue(any("do not normally re-gate" in r for r in decision.reasons))

    def test_threshold_is_read_from_config_not_hardcoded(self):
        decision = gate.decide(approved(est_p95_usd="12.00"), config(threshold=25.0))
        self.assertEqual(decision.action, gate.ALLOW)


class ApiRailBlock(unittest.TestCase):
    """TASK-012, 6 September 2026. The api rail was a downgrade to ask; it is
    now a block. The owner's judgement: "unreachable by accident is a fail" -- an
    ask is answerable at 07:00 by nobody, and the objection to the paid rail is
    the currency, not the amount.

    Every test here passes the rail as a string. The gate is pure, so its tests
    stay pure by construction: if one of these needed an environment, the
    design would have drifted."""

    def optin(self, **extra):
        front = approved(est_p95_usd="1.00",
                         api_rail_allowed_by="Ada",
                         api_rail_reason="one-off reproducibility check",
                         api_rail_cap_usd="3.00")
        front.update(extra)
        return front

    # ------------------------------------------------------------ absent --

    def test_the_api_rail_blocks_with_no_opt_in_at_all(self):
        decided = gate.decide(approved(est_p95_usd="1.00"), config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIsNone(decided.api_rail_optin)

    def test_the_block_names_all_three_fields_and_their_exit_tests(self):
        decided = gate.decide(approved(est_p95_usd="1.00"), config(), rail="api")
        text = " ".join(decided.reasons)
        for field in gate.API_RAIL_FIELDS:
            self.assertIn(field, text)
            self.assertIn(gate.API_RAIL_EXIT_TESTS[field], text)

    def test_a_partial_set_names_exactly_what_is_missing(self):
        front = self.optin()
        del front["api_rail_reason"]
        decided = gate.decide(front, config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        text = " ".join(decided.reasons)
        self.assertIn("api_rail_reason", text)
        self.assertIn("1 field(s)", text)
        self.assertNotIn("'api_rail_allowed_by'", text)

    def test_a_null_field_is_missing_rather_than_present(self):
        # Front matter is flat text and the template writes `null` for unset.
        decided = gate.decide(self.optin(api_rail_allowed_by="null"), config(),
                              rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("api_rail_allowed_by", " ".join(decided.reasons))

    # ---------------------------------------------------------- the cap ---

    def test_a_malformed_cap_blocks_naming_the_bad_value(self):
        decided = gate.decide(self.optin(api_rail_cap_usd="thre dollars"),
                              config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("thre dollars", " ".join(decided.reasons))

    def test_a_cap_above_the_ceiling_blocks(self):
        decided = gate.decide(self.optin(api_rail_cap_usd="9.00"),
                              config(ceiling=5.0), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("api_rail_cap_ceiling_usd", " ".join(decided.reasons))

    def test_a_cap_exactly_at_the_ceiling_is_allowed_through_to_the_ask(self):
        decided = gate.decide(self.optin(api_rail_cap_usd="5.00"),
                              config(ceiling=5.0), rail="api")
        self.assertEqual(decided.action, gate.ASK)

    def test_an_estimate_above_the_cap_blocks(self):
        decided = gate.decide(self.optin(est_p95_usd="4.00",
                                         api_rail_cap_usd="3.00"),
                              config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("never lower the estimate", " ".join(decided.reasons))

    def test_an_estimate_exactly_at_the_cap_asks(self):
        decided = gate.decide(self.optin(est_p95_usd="3.00",
                                         api_rail_cap_usd="3.00"),
                              config(), rail="api")
        self.assertEqual(decided.action, gate.ASK)

    def test_an_unpriceable_run_blocks_rather_than_asks_on_the_api_rail(self):
        front = self.optin()
        del front["est_p95_usd"]
        decided = gate.decide(front, config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("cannot be priced", " ".join(decided.reasons))

    def test_a_config_with_no_ceiling_blocks_rather_than_inventing_one(self):
        broken = config()
        del broken["api_rail_cap_ceiling_usd"]
        decided = gate.decide(self.optin(), broken, rail="api")
        self.assertEqual(decided.action, gate.BLOCK)

    # ------------------------------------------------------- a complete --

    def test_a_complete_opt_in_is_an_ask_and_never_an_allow(self):
        decided = gate.decide(self.optin(), config(), rail="api")
        self.assertEqual(decided.action, gate.ASK)
        self.assertIn("never an allow", " ".join(decided.reasons))

    def test_a_complete_opt_in_is_carried_for_the_decisions_log(self):
        decided = gate.decide(self.optin(), config(), rail="api")
        self.assertEqual(decided.api_rail_optin["allowed_by"], "Ada")
        self.assertEqual(decided.api_rail_optin["cap_usd"], 3.00)
        self.assertEqual(decided.api_rail_optin["ceiling_usd"], 5.00)
        self.assertEqual(decided.api_rail_optin["estimate_usd"], 1.00)

    def test_a_complete_opt_in_under_the_threshold_still_does_not_allow(self):
        # $1.00 is far under the $10 threshold. On the subscription rail this
        # is the ALLOW case; the rail is checked above the threshold because
        # the objection is the currency, not the amount.
        self.assertEqual(gate.decide(self.optin(), config()).action, gate.ALLOW)
        self.assertEqual(gate.decide(self.optin(), config(), rail="api").action,
                         gate.ASK)

    # ---------------------------------------------------------- ordering --

    def test_state_is_checked_above_the_rail(self):
        front = self.optin()
        front["state"] = "drafted"
        decided = gate.decide(front, config(), rail="api")
        self.assertEqual(decided.action, gate.BLOCK)
        self.assertIn("not 'approved'", " ".join(decided.reasons))
        self.assertEqual(decided.warnings, [])

    def test_the_subscription_rail_changes_nothing(self):
        decided = gate.decide(approved(est_p95_usd="1.00"), config(),
                              rail="subscription")
        self.assertEqual(decided.action, gate.ALLOW)
        self.assertEqual(decided.warnings, [])
        self.assertIsNone(decided.api_rail_optin)

    def test_no_rail_at_all_changes_nothing(self):
        self.assertEqual(gate.decide(approved(est_p95_usd="1.00"), config()).action,
                         gate.ALLOW)

    def test_the_opt_in_fields_are_ignored_on_the_subscription_rail(self):
        decided = gate.decide(self.optin(api_rail_cap_usd="nonsense"), config(),
                              rail="subscription")
        self.assertEqual(decided.action, gate.ALLOW)

    def test_there_is_no_escalate_for_rail_any_more(self):
        # The downgrade-to-ask function is gone rather than left beside the
        # block. Two answers to one question is two things to keep true.
        self.assertFalse(hasattr(gate, "escalate_for_rail"))


class ApiRailCeilingConfig(unittest.TestCase):
    def test_the_shipped_config_carries_a_ceiling(self):
        loaded = gate.load_config()
        self.assertIsInstance(loaded["api_rail_cap_ceiling_usd"], (int, float))
        self.assertGreater(loaded["api_rail_cap_ceiling_usd"], 0)

    def test_the_shipped_config_holds_no_standing_permission(self):
        # gate.json gains a ceiling and nothing else. A standing allowance here
        # would turn "reachable by accident" into "reachable by forgetting".
        loaded = gate.load_config()
        for field in gate.API_RAIL_FIELDS:
            self.assertNotIn(field, loaded)

    def test_a_config_without_a_ceiling_is_fatal_rather_than_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gate.json"
            path.write_text(json.dumps({"threshold_usd": 10.0,
                                        "min_samples_for_prior": 3}))
            with self.assertRaises(gate.GateConfigError):
                gate.load_config(path)

    def test_a_non_positive_ceiling_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gate.json"
            path.write_text(json.dumps({"threshold_usd": 10.0,
                                        "min_samples_for_prior": 3,
                                        "api_rail_cap_ceiling_usd": 0}))
            with self.assertRaises(gate.GateConfigError):
                gate.load_config(path)


if __name__ == "__main__":
    unittest.main()
