#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The budget gate. Pure logic: no environment, no filesystem beyond loading
gate.json, no network. Everything here is a function of its arguments so it can
be tested without a Claude run.

Per spec-v0.2.md 7, the gate is the only part of the wrapper that must sit in
the path of a run. Everything else -- cost, lineage, session resumption, the
dashboard's data -- is satisfied by reading the record afterwards.

The decision is one of allow / ask / block. There is no silent pass: a run this
module cannot price is an ASK, stating why, not an assumption.

Threshold and priors are data, in gate.json, the way prices.json and rails.json
already work. Adjust them there, never here.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE_PATH = HERE / "gate.json"

ALLOW = "allow"
ASK = "ask"
BLOCK = "block"

# Why an estimate could not be made. Carried on the decision so the output can
# say which of the three paths it took rather than just quoting a number.
SOURCE_FRONT_MATTER = "front_matter"
SOURCE_PRIOR = "prior"
SOURCE_NONE = "none"

# --------------------------------------------------------------- the rail ----
# §5.3: subscription and API credit are separate rails, not one balance. From
# TASK-012 (6 September 2026) an `api` rail is a BLOCK, not a downgrade to ask.
# The owner's judgement that day: "unreachable by accident is a fail." TASK-009 had
# established that the paid rail is currently unreachable on this machine -- no
# key, no OAuth, no keychain item -- but that is a property of the machine's
# present state, not a control, and it evaporates the moment a key is set for
# some unrelated reason.
#
# The opt-in is per-run, hand-written on the task file, and carries all three of
# these together. It deliberately does NOT live in gate.json: a standing config
# allowance converts "reachable by accident" into "reachable by forgetting",
# which is the same failure with a longer fuse. gate.json holds the shape and
# the ceiling on the cap; the consent is per-run.
#
# A complete opt-in resolves to ASK, never ALLOW. The owner still answers at run
# time -- the fields say the run may be considered, not that it may proceed.
RAIL_API = "api"

API_RAIL_FIELDS = ("api_rail_allowed_by", "api_rail_reason", "api_rail_cap_usd")

# Each debt in the shape store.py's stage gates use: the field, where the
# obligation comes from, and the test that discharges it. A gate that says no
# without saying what it wants is the flat refusal §3.1 rejects.
API_RAIL_EXIT_TESTS = {
    "api_rail_allowed_by": "a person's name -- who is answering for this run's spend",
    "api_rail_reason": "one line saying why this run needs the paid rail rather "
                       "than the subscription",
    "api_rail_cap_usd": "a positive number of dollars this run may not exceed, at "
                        "or under api_rail_cap_ceiling_usd in gate.json",
}

# Stated on every ask that fails to estimate. Run-level cost IS derivable now --
# ledger/recorder.py writes a Run record per session id and prices it (Phase 1
# an earlier session, landed 5 September 2026) -- so an unpriced task class means no
# session has been linked to that class yet, not that the machinery is missing.
# The fix is a linked run in ledger/run-tasks.json, then re-derive.
NO_RUN_LEVEL_COST_NOTE = (
    "Run-level cost is derivable: `python3 ledger/recorder.py` writes a Run "
    "record per session with its own cost_usd. A class with no usable prior "
    "has fewer than min_samples_for_prior linked runs -- link them in "
    "ledger/run-tasks.json and re-derive with `--priors --write-priors`."
)


class GateConfigError(Exception):
    """gate.json is missing or unusable. Never fall back to a default
    threshold: a gate that invents its own limit is not a gate."""


def load_config(path=GATE_PATH):
    """Load gate.json. A missing or broken file is fatal, deliberately."""
    path = Path(path)
    if not path.exists():
        raise GateConfigError(f"No gate config at {path}. Refusing to guess a threshold.")
    try:
        config = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise GateConfigError(f"Cannot read {path}: {exc}") from exc

    threshold = config.get("threshold_usd")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise GateConfigError(f"{path} has no numeric threshold_usd.")
    if threshold <= 0:
        raise GateConfigError(f"{path} has threshold_usd {threshold}; must be positive.")

    minimum = config.get("min_samples_for_prior")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise GateConfigError(f"{path} has no usable min_samples_for_prior.")

    # The ceiling on a per-run API-rail cap. Fatal when missing, for the same
    # reason the threshold is: a gate that invents its own limit is not a gate,
    # and this one bounds the rail that bills real money at list price.
    ceiling = config.get("api_rail_cap_ceiling_usd")
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        raise GateConfigError(
            f"{path} has no numeric api_rail_cap_ceiling_usd. It is the ceiling "
            f"on a per-run api_rail_cap_usd, not a permission -- the consent is "
            f"per-run and hand-written on the task file (§5.3, TASK-012).")
    if ceiling <= 0:
        raise GateConfigError(
            f"{path} has api_rail_cap_ceiling_usd {ceiling}; must be positive.")
    return config


# ------------------------------------------------------------- estimate ----

class Estimate:
    """An estimated p95 cost in USD, and where it came from.

    usd is None when no estimate could be made at all. That is a real answer
    and it forces an ask; it is not an error and it is not zero."""

    __slots__ = ("usd", "source", "basis")

    def __init__(self, usd, source, basis):
        self.usd = usd
        self.source = source
        self.basis = basis

    def __repr__(self):  # pragma: no cover - debugging convenience
        return f"Estimate(usd={self.usd!r}, source={self.source!r})"

    def as_dict(self):
        return {"usd": self.usd, "source": self.source, "basis": self.basis}


def _as_positive_float(value):
    """Coerce a front-matter string to a positive float, or None. Front matter
    is flat text, so everything arrives as a string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number if number >= 0 else None


def prior_for(task_class, config):
    """The p95 prior for a task class, or None with the reason it is unusable.

    Returns (usd, note). usd is None whenever the class is unknown or has fewer
    than min_samples_for_prior samples -- an average of two runs is not a p95,
    and saying so is cheaper than a wrong number.

    With samples this few, p95 is taken as the worst sample observed. It is a
    ceiling on what has actually happened, not a fitted quantile, and it is
    labelled as such wherever it is printed."""
    minimum = config["min_samples_for_prior"]
    priors = config.get("priors") or {}
    if not task_class:
        return None, "no task class on the task file, so no prior to look up"
    prior = priors.get(task_class)
    if prior is None:
        known = ", ".join(sorted(priors)) or "none"
        return None, f"no prior for task class '{task_class}' in gate.json (known: {known})"

    samples = [s for s in (_as_positive_float(v) for v in prior.get("samples_usd") or []) if s is not None]
    n = len(samples)
    declared = prior.get("n")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared != n:
        return None, (f"prior for '{task_class}' declares n={declared} but carries "
                      f"{n} usable sample(s); fix gate.json rather than trusting either")
    if n < minimum:
        return None, (f"prior for '{task_class}' has n={n}, below "
                      f"min_samples_for_prior={minimum}")
    return max(samples), (f"worst of {n} observed run(s) for '{task_class}', "
                          f"used as p95; see gate.json priors_basis")


def estimate_cost(front_matter, config):
    """Estimate p95 cost in USD for a task, in the order settled on 5 September:

      1. est_p95_usd in the task file's front matter. The owner preflights every
         run and writes the estimate down, so his own number is the best one
         available and it is used first.
      2. otherwise the prior for the task's class in gate.json, but only where
         n >= min_samples_for_prior.
      3. otherwise no estimate, which is an ask.

    Path 2 became real on 5 September 2026 when the Recorder landed and priors
    stopped being hand-carried; it is still only as good as the number of runs
    linked to a class in ledger/run-tasks.json. The output says which path it
    took, every time."""
    declared = front_matter.get("est_p95_usd")
    if declared is not None:
        usd = _as_positive_float(declared)
        if usd is not None:
            return Estimate(usd, SOURCE_FRONT_MATTER,
                            "est_p95_usd from the task file's front matter")
        # Present but unusable. Do not fall through quietly to a prior -- a
        # malformed estimate is a typo in a number the gate is about to act on.
        return Estimate(None, SOURCE_NONE,
                        f"est_p95_usd is present but not a positive number: {declared!r}")

    task_class = front_matter.get("task_class") or front_matter.get("task_type")
    usd, note = prior_for(task_class, config)
    if usd is not None:
        return Estimate(usd, SOURCE_PRIOR, note)
    return Estimate(None, SOURCE_NONE,
                    f"no est_p95_usd in the task file, and {note}")


# ------------------------------------------------------------- the rail ----

def _text(value):
    """A front-matter string, stripped, or "" for anything blank or absent.
    Front matter is flat text and `null` is how the template writes "unset"."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("", "null", "none", "~") else text


def api_rail_debts(front_matter):
    """The opt-in fields this task file still owes, in store.py's debt shape.

    All three are required together and none of them is defaulted. A partial
    set is a refusal naming exactly what is missing and the test that discharges
    it -- the same form the stage gates use, because a gate that says no without
    saying what it wants is the flat refusal §3.1 rejects."""
    return [{"field": field,
             "owed_from": "the api rail (§5.3, TASK-012)",
             "exit_test": API_RAIL_EXIT_TESTS[field]}
            for field in API_RAIL_FIELDS
            if not _text(front_matter.get(field))]


def _format_debt(debt):
    """store.format_debt's wording, kept identical on purpose. The gate is pure
    and does not import the store, but two phrasings of one debt would be two
    things to keep true."""
    return f"{debt['field']!r} (from {debt['owed_from']!r}: {debt['exit_test']})"


def check_api_rail(front_matter, config, estimate):
    """Consider an API-rail run's per-run opt-in.

    Returns (refusals, optin). A non-empty `refusals` is a BLOCK; otherwise
    `optin` is the consent record the caller should log and the answer is an
    ASK, never an ALLOW.

    Pure, like everything here: the rail arrives as a string. Detection lives in
    launch.py, which is where the environment is."""
    debts = api_rail_debts(front_matter)
    if debts:
        return ([f"the api rail needs a per-run opt-in on the task file, and this "
                 f"one owes {len(debts)} field(s): " +
                 "; ".join(_format_debt(d) for d in debts) +
                 ". The opt-in is per-run and hand-written; it cannot live in "
                 "gate.json, because a standing allowance turns 'reachable by "
                 "accident' into 'reachable by forgetting'."], None)

    raw_cap = front_matter.get("api_rail_cap_usd")
    cap = _as_positive_float(raw_cap)
    if cap is None or cap == 0:
        return ([f"api_rail_cap_usd is present but not a positive number: "
                 f"{raw_cap!r}. On the paid rail a cap that cannot be read is a "
                 f"refusal, not a fallback."], None)

    ceiling = float(config.get("api_rail_cap_ceiling_usd") or 0)
    if ceiling <= 0:
        return (["gate.json carries no usable api_rail_cap_ceiling_usd, so this "
                 "cap cannot be bounded. A gate that invents its own limit is "
                 "not a gate."], None)
    if _cents(cap) > _cents(ceiling):
        return ([f"api_rail_cap_usd ${cap:.2f} is above the "
                 f"${ceiling:.2f} api_rail_cap_ceiling_usd in gate.json. Raise "
                 f"the ceiling deliberately, in data, with a reason -- do not "
                 f"talk the cap down to fit."], None)

    if estimate.usd is None:
        return ([f"the api rail bills real money at list price and this run "
                 f"cannot be priced: {estimate.basis}. An unpriced run cannot be "
                 f"checked against its ${cap:.2f} cap, so it is refused rather "
                 f"than asked about."], None)
    if _cents(estimate.usd) > _cents(cap):
        return ([f"estimated p95 ${estimate.usd:.2f} is above the ${cap:.2f} "
                 f"api_rail_cap_usd on the task file ({estimate.basis}). Raise "
                 f"the cap deliberately if the run is worth it -- never lower "
                 f"the estimate to make the cap allow."], None)

    optin = {
        "allowed_by": _text(front_matter.get("api_rail_allowed_by")),
        "reason": _text(front_matter.get("api_rail_reason")),
        "cap_usd": cap,
        "ceiling_usd": ceiling,
        "estimate_usd": estimate.usd,
    }
    return ([], optin)


# ------------------------------------------------------------- decision ----

class Decision:
    __slots__ = ("action", "reasons", "estimate", "threshold_usd", "warnings",
                 "api_rail_optin")

    def __init__(self, action, reasons, estimate, threshold_usd, warnings=None,
                 api_rail_optin=None):
        self.action = action
        self.reasons = list(reasons)
        self.estimate = estimate
        self.threshold_usd = threshold_usd
        self.warnings = list(warnings or [])
        # The per-run API-rail consent this decision consumed, or None. Set only
        # where all three fields were present and usable and the cap held. It is
        # carried rather than written here because the gate is pure: launch.py
        # appends it to decisions/*.jsonl, which is where side effects live.
        self.api_rail_optin = api_rail_optin

    @property
    def allowed(self):
        return self.action == ALLOW

    def as_dict(self):
        return {
            "action": self.action,
            "reasons": self.reasons,
            "estimate": self.estimate.as_dict(),
            "threshold_usd": self.threshold_usd,
            "warnings": self.warnings,
            "api_rail_optin": self.api_rail_optin,
        }


def _cents(usd):
    """Compare money in cents. Two floats that both print as $10.00 must
    compare equal at the threshold."""
    return round(usd * 100)


def decide(front_matter, config, *, scheduled=False, estimate=None, rail=None):
    """allow / ask / block for one task.

    Order, and the reasoning for each:

      block  the task is not approved. A gate spends a budget against an
             approved task; it does not approve one. Nothing to ask about here
             because the answer belongs on the task file, not in this prompt.
      block  the run would bill the api rail and the per-run opt-in is missing,
             incomplete, malformed, or its cap does not cover the estimate.
      ask    the run would bill the api rail with a complete opt-in. Never an
             allow: the fields say the run may be considered, not that it may
             proceed, and the owner answers at run time.
      ask    no estimate could be made. Stated with the reason, per 5.5's
             "estimate from history, never from prompt length" -- and with no
             history, the honest answer is a question.
      ask    the estimate is above threshold_usd. This is the case gate.json
             exists for: a scheduled run is approved when it is scheduled and
             does not re-gate (5.5), EXCEPT above the standing threshold.
      allow  approved, priced, at or under threshold, on the subscription rail.

    State sits above the rail because an unapproved task has nothing to say
    about rails either way. The rail sits above the threshold because the
    objection to the api rail is the currency, not the amount -- an api-rail run
    is refused before its price is considered.

    The threshold boundary is deliberate: exactly threshold_usd allows. The
    threshold is the point above which a run re-gates, per threshold_basis.

    `rail` is a string the caller supplies, not an environment this module
    inspects. Keeping it a field is what keeps the gate pure and its tests pure
    by construction -- if a rail test needs an environment, the design has
    drifted."""
    if estimate is None:
        estimate = estimate_cost(front_matter, config)
    threshold = float(config["threshold_usd"])
    state = (front_matter.get("state") or "").strip().lower()

    if state != "approved":
        shown = state or "unset"
        return Decision(BLOCK,
                        [f"task state is '{shown}', not 'approved'. The gate spends "
                         f"an approved budget; it does not approve the task."],
                        estimate, threshold)

    if rail == RAIL_API:
        warning = (
            "this run would bill the API rail at list price, not the subscription "
            "(§5.3). The gate.json threshold was set against subscription-rail "
            "runs and does not transfer.")
        refusals, optin = check_api_rail(front_matter, config, estimate)
        if refusals:
            return Decision(BLOCK, [warning] + refusals, estimate, threshold,
                            [warning])
        return Decision(ASK,
                        [warning,
                         f"a per-run opt-in is on the task file: allowed by "
                         f"{optin['allowed_by']}, capped at ${optin['cap_usd']:.2f}, "
                         f"because {optin['reason']}. Estimated p95 "
                         f"${optin['estimate_usd']:.2f} is within the cap.",
                         "A complete opt-in is an ask, never an allow. It says the "
                         "run may be considered; you still answer at run time."],
                        estimate, threshold, [warning], api_rail_optin=optin)

    if estimate.usd is None:
        reasons = [f"cannot estimate: {estimate.basis}.", NO_RUN_LEVEL_COST_NOTE]
        return Decision(ASK, reasons, estimate, threshold)

    if _cents(estimate.usd) > _cents(threshold):
        reasons = [f"estimated p95 ${estimate.usd:.2f} is above the ${threshold:.2f} "
                   f"threshold in gate.json ({estimate.basis})."]
        if scheduled:
            reasons.append("Scheduled runs do not normally re-gate (5.5). This one does, "
                           "because it is above the standing threshold.")
        return Decision(ASK, reasons, estimate, threshold)

    return Decision(ALLOW,
                    [f"estimated p95 ${estimate.usd:.2f} is at or under the "
                     f"${threshold:.2f} threshold ({estimate.basis})."],
                    estimate, threshold)
