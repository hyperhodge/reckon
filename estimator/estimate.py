#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Break a proposal down and return a cost RANGE with its own hit rate.

P-0001-T45. The owner named this as the product on 10 September 2026: "so what we really want is
to break down the concept of a proposal (any proposal) and return a cost".

THE SHAPE OF THE MODEL IS THE HONEST PART. It is two stages with very different amounts of
evidence behind them, and they are kept apart so the weak one cannot hide inside the strong one:

  1. WORDS -> TURNS.  How many assistant messages a piece of work will take. Fitted on 27
     (label, turns) pairs from ledger/preflight-ledger.md. THIS IS THE WEAK STAGE and it is
     where nearly all the uncertainty lives.
  2. TURNS -> COST.   Fitted on 46 Run records from ledger/runs.json. Cost per assistant
     message is flat below about 110 messages and rises above it, so this is a curve and not
     a rate. THIS IS THE STRONG STAGE: actual/predicted sits inside 0.65-1.25.

NEVER RETURNS A POINT ESTIMATE. Every answer is an interval carrying the measured out-of-sample
hit rate of that interval, from --backtest. A confidently wrong number is the failure this was
built to avoid: two of the first five runs in the preflight ledger overran the high end of a
stated range, which is the baseline this has to beat.

EVERY FIGURE IS A CLIENT-SIDE ESTIMATE AT LIST RATES, inherited from the Run records it is
fitted on, and it is a FLOOR on what an account loses rather than what anything cost. Never
present it as a bill, and never convert it to sterling -- this register holds no FX rate
(store.COST_TO_DATE_GBP_RULE).

Parameters are data in estimator/model.json, derived by --fit, never hand-edited.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODEL_PATH = HERE / "model.json"
LEDGER_PATH = ROOT / "ledger" / "preflight-ledger.md"
RUNS_PATH = ROOT / "ledger" / "runs.json"

# The ceiling is data in wrapper/gate.json and is read, never duplicated here.
GATE_PATH = ROOT / "wrapper" / "gate.json"


# ---------------------------------------------------------------- feature extraction

# Signals are words, because the input is words. Each one is here because it separates the
# high-turn rows in preflight-ledger.md from the low-turn ones, and the fitted weight says
# how much -- if a weight comes out near zero, the signal was a guess and the fit says so.
ENFORCEMENT_WORDS = (
    "enforce", "enforcement", "refuse", "refuses", "guard", "gate", "consent",
    "block", "blocks", "integrity", "append-only", "never", "every ",
)
SCREEN_WORDS = (
    "front end", "screen", "app/", "pwa", "board", "dashboard", "panel", "page",
    "button", "mobile", "phone", "visible", "shows", "render",
)
INVESTIGATION_WORDS = (
    "does ", "check whether", "rail check", "find out", "question", "investigate",
    "measure", "reconcile", "calibrat",
)
WRITING_WORDS = ("document", "documentation", "readme", "manual", "write-up", "notes", "prose")
SCHEMA_WORDS = ("schema", "field", "record", "store", "table", "json", "query", "queries")

_SPLIT = re.compile(
    r"(?:\n+|[;•]|\s+—\s+|\s-\s|(?<=[a-z)])\,\s+(?:and\s+)?|\s+and\s+then\s+|\s+then\s+)",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "nineteen": 19,
}


def split_items(text: str) -> list[str]:
    """Break a proposal into the pieces a session would actually do.

    He does not do the breakdown -- he said so on 10 September, and it is the same requirement
    as the capture box proposing where his words fit (P-0001-T30). This is deliberately a dumb
    splitter: it is honest about being one, and an item it gets wrong is visible in the output
    because every item is printed with its own estimate.
    """
    parts = [p.strip(" .\t") for p in _SPLIT.split(text or "") if p and p.strip(" .\t")]
    # Fragments of four characters or fewer are punctuation noise, not work.
    parts = [p for p in parts if len(p) > 4]
    return parts or [(text or "").strip()]


def counted_multiplicity(text: str) -> int:
    """An explicit count in the words is worth more than a conjunction.

    "the 16 acceptance queries" took 112 turns; "four items" took 30. A number attached to a
    plural noun is the single clearest size signal in the ledger, so it is read directly
    rather than inferred -- capped, because the relationship is sublinear and an uncapped
    count would let one phrase dominate the whole estimate.
    """
    low = (text or "").lower()
    best = 1
    # Up to two words may sit between the number and its plural noun: "the 16 acceptance
    # queries", "four open risk items".
    plural = r"(?:[a-z][a-z-]+\s+){0,2}[a-z][a-z-]+s\b"
    for m in re.finditer(rf"\b(\d{{1,2}})\s+{plural}", low):
        best = max(best, int(m.group(1)))
    for word, n in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+{plural}", low):
            best = max(best, n)
    return min(best, 16)


def features(text: str) -> dict:
    low = (text or "").lower()
    items = split_items(text)
    return {
        "items": len(items),
        "counted": counted_multiplicity(text),
        "enforcement": 1 if any(w in low for w in ENFORCEMENT_WORDS) else 0,
        "screen": 1 if any(w in low for w in SCREEN_WORDS) else 0,
        "investigation": 1 if any(w in low for w in INVESTIGATION_WORDS) else 0,
        "writing": 1 if any(w in low for w in WRITING_WORDS) else 0,
        "schema": 1 if any(w in low for w in SCHEMA_WORDS) else 0,
        "words": len(low.split()),
    }


def design_row(f: dict) -> list[float]:
    """The model is linear in log-turns. Size enters as a log so it is sublinear: doing four
    things in one session has never cost four times one thing, because the context is shared.
    """
    return [
        1.0,
        math.log(max(f["items"], 1)),
        math.log(max(f["counted"], 1)),
        float(f["enforcement"]),
        float(f["screen"]),
        float(f["investigation"]),
        float(f["writing"]),
        math.log(max(f["words"], 1)),
    ]


TERMS = ["const", "log_items", "log_counted", "enforcement", "screen",
         "investigation", "writing", "log_words"]


# ---------------------------------------------------------------- linear algebra

def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. Small and dependency-free on purpose."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular design matrix")
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col] / a[col][col]
            for k in range(col, n + 1):
                a[row][k] -= factor * a[col][k]
    return [a[i][n] / a[i][i] for i in range(n)]


def ridge_fit(rows: list[list[float]], ys: list[float], lam: float) -> list[float]:
    """Ridge, because there are 27 samples and eight terms.

    Unpenalised least squares on this many samples fits the noise and then reports a narrow
    interval it has not earned. The penalty is on every term except the constant.
    """
    k = len(rows[0])
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
    for i in range(1, k):
        xtx[i][i] += lam
    return _solve(xtx, xty)


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ---------------------------------------------------------------- training data

def read_turn_samples(path: Path = LEDGER_PATH) -> list[dict]:
    """(words, turns) pairs from the preflight ledger's own rows.

    The label column is the closest thing this project has to "a proposal in his words at the
    moment of asking": it is what the session was told to do, written before the work started.
    Turns are the assistant-message count recorded on the same row.
    """
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\d{4}-\d\d-\d\d \|", line):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 6:
            continue
        label, turn_cell = cells[2], cells[5]
        match = re.search(r"(\d+)", turn_cell)
        if not match:
            continue  # "~N turns" -- no number was ever recorded, so it is not a sample
        samples.append({"label": label, "turns": int(match.group(1)), "class": cells[1]})
    return samples


def read_cost_samples(path: Path = RUNS_PATH) -> list[dict]:
    """(turns, cost) pairs from Run records. Cost is copied from the record, never re-priced."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for run in data.get("runs", []):
        msgs = run.get("assistant_messages") or 0
        cost = run.get("cost_usd") or 0.0
        # Below eight messages a session is an orientation or a crash, not a piece of work;
        # including them drags the curve down at the short end where nothing is ever asked.
        if msgs >= 8 and cost > 0 and run.get("model") == "claude-opus-5":
            out.append({"ref": run["ref"], "turns": msgs, "cost_usd": cost})
    return out


# ---------------------------------------------------------------- fitting

def fit_cost_curve(samples: list[dict]) -> dict:
    """cost = a*m + b*m^2/100, plus the empirical spread of actual/predicted.

    Quadratic because the per-message cost is not flat: context accumulates inside a session,
    so message 150 is priced on a longer prompt than message 15. The spread is carried as
    quantiles of the RATIO rather than a standard deviation, because it is visibly asymmetric.
    """
    rows = [[s["turns"], s["turns"] ** 2 / 100.0] for s in samples]
    ys = [s["cost_usd"] for s in samples]
    a, b = ridge_fit(rows, ys, lam=0.0)
    ratios = sorted(s["cost_usd"] / (a * s["turns"] + b * s["turns"] ** 2 / 100.0)
                    for s in samples)
    return {
        "a_per_message": a,
        "b_per_message_squared_over_100": b,
        "ratio_quantiles": {q: quantile(ratios, float(q)) for q in ("0.1", "0.5", "0.9")},
        "n": len(samples),
    }


def _loo_residuals(rows: list[list[float]], ys: list[float], lam: float) -> list[float]:
    out = []
    for i in range(len(ys)):
        coef = ridge_fit(rows[:i] + rows[i + 1:], ys[:i] + ys[i + 1:], lam)
        out.append(ys[i] - sum(x * y for x, y in zip(rows[i], coef)))
    return out


# The candidates, narrowest first. A richer model has to EARN its place by predicting better
# out of sample, and on this evidence most of them do not -- which is a finding about the words
# and not a bug in the fit.
CANDIDATES = {
    "median_only": [],
    "items": ["log_items"],
    "words": ["log_words"],
    "items_and_words": ["log_items", "log_words"],
    "all_eight": [t for t in TERMS if t != "const"],
}


def _rows_for(samples: list[dict], terms: list[str]) -> list[list[float]]:
    rows = []
    for s in samples:
        full = dict(zip(TERMS, design_row(features(s["label"]))))
        rows.append([1.0] + [full[t] for t in terms])
    return rows


def fit_turn_model(samples: list[dict], lam: float) -> dict:
    """Choose the feature set by out-of-sample error, not by taste.

    SELECTION IS ON PREDICTION ERROR AND NEVER ON THE HIT RATE. Tuning the model until the
    interval looks good is how an estimator ends up confidently wrong: the width is then chosen
    to flatter the width. So the winner is the one with the lowest leave-one-out error, and
    whatever hit rate falls out of it is reported as it comes.
    """
    ys = [math.log(s["turns"]) for s in samples]
    scored = {}
    for name, terms in CANDIDATES.items():
        rows = _rows_for(samples, terms)
        use_lam = 0.0 if len(terms) <= 1 else lam
        res = _loo_residuals(rows, ys, use_lam)
        scored[name] = {
            "terms": terms,
            "lambda": use_lam,
            "loo_rmse_log": math.sqrt(sum(r * r for r in res) / len(res)),
        }
    winner = min(scored, key=lambda k: scored[k]["loo_rmse_log"])
    terms = scored[winner]["terms"]
    lam_used = scored[winner]["lambda"]
    rows = _rows_for(samples, terms)
    coef = ridge_fit(rows, ys, lam_used)
    loo = _loo_residuals(rows, ys, lam_used)
    loo_sorted = sorted(loo)
    null_rmse = scored["median_only"]["loo_rmse_log"]
    chosen_rmse = scored[winner]["loo_rmse_log"]
    return {
        "selected": winner,
        "terms": terms,
        "coefficients": dict(zip(["const"] + terms, coef)),
        "lambda": lam_used,
        "n": len(samples),
        "candidates_loo_rmse_log": {k: round(v["loo_rmse_log"], 4) for k, v in scored.items()},
        "improvement_over_median_only": round(1.0 - chosen_rmse / null_rmse, 4),
        "loo_log_residual_quantiles": {
            q: quantile(loo_sorted, float(q)) for q in ("0.1", "0.5", "0.9")
        },
        "loo_log_residual_sd": math.sqrt(sum(r * r for r in loo) / len(loo)),
        "loo_median_abs_ratio_error": math.exp(
            quantile(sorted(abs(r) for r in loo), 0.5)) - 1.0,
        "_what_the_selection_means": (
            "THE WORDS BARELY PREDICT THE SIZE. Every candidate sits within a few per cent of "
            "predicting the historical median and ignoring the proposal entirely, and the "
            "eight-feature model is markedly WORSE out of sample. Two reasons, and neither is "
            "fixable by trying harder on the text. (1) The training labels are short titles, "
            "five to fifteen words, because that is what the ledger recorded. (2) Turn count "
            "is partly a CHOICE: a session ran until it was done, interrupted, or out of "
            "ceiling, so the thing being predicted is not a property of the work alone. Read "
            "the per-item number as the historical median, lightly adjusted, and read the "
            "range as what that history actually did."
        ),
    }


def fit_realisation(model_so_far: dict) -> dict:
    """The two corrections that stand between a turn count and what the project actually spent.

    THIS IS THE MOST IMPORTANT BLOCK IN THE MODEL AND IT WAS FOUND BY CHECKING, NOT BY DESIGN.
    The bottom-up estimate priced this register's whole remaining programme at about $240, while
    the register has spent $414 on p2p-register to ship twenty-three items. A number 3.6 times
    under what the same work has repeatedly cost is the confidently-wrong answer this task was
    written to avoid, so the gap is measured and applied rather than explained away.

    It decomposes cleanly into two factors that are measured separately and are close to equal:

      1. BOTTOM-UP -> DIRECT, about 1.55x. Feed a shipped task's own brief to the estimator and
         compare with what its session actually cost. The model reads a brief and predicts the
         median piece of work; a task is bigger than the median row in the training ledger,
         because that ledger also holds sub-items that were never tasks.
      2. DIRECT -> REALISED, about 1.51x. The cost of the session that did the work is not the
         cost of the work. Handoffs, orientation, decisions, re-work, the conversation that
         found the next thing, and the sessions that shipped nothing are all real spend against
         the same shipped items. Total project spend divided by items shipped, over the cost of
         the sessions that can be attributed to a task.

    Neither is a contingency and neither is padding. They are ratios between two measured
    figures, and the second one says something worth reading on its own: ABOUT A THIRD OF WHAT
    THIS PROJECT COST WAS NOT THE WORK.
    """
    runs = json.loads(RUNS_PATH.read_text(encoding="utf-8"))["runs"]
    links = json.loads((ROOT / "ledger" / "run-tasks.json").read_text(encoding="utf-8"))["map"]
    by_session = {r["claude_session_id"]: r for r in runs}

    direct: dict[str, float] = {}
    for session_id, entry in links.items():
        ref = entry.get("task_ref") if isinstance(entry, dict) else entry
        run = by_session.get(session_id)
        if ref and run:
            direct[ref] = direct.get(ref, 0.0) + (run.get("cost_usd") or 0.0)

    ratios = []
    per_task = []
    for ref, actual in sorted(direct.items()):
        task_file = ROOT / "tasks" / f"{ref}.md"
        if not task_file.exists():
            continue
        front = task_file.read_text(encoding="utf-8").split("---")[1]
        brief_match = re.search(r"^brief:\s*(.+)$", front, re.MULTILINE)
        if not brief_match:
            continue
        predicted = estimate(brief_match.group(1), model_so_far, apply_realisation=False)
        p50 = predicted["cost_usd"]["p50"]
        ratios.append(actual / p50)
        per_task.append({"task": ref, "direct_actual_usd": round(actual, 2),
                         "bottom_up_p50_usd": round(p50, 2), "ratio": round(actual / p50, 2)})

    ratios.sort()
    bottom_up_to_direct = quantile(ratios, 0.5)

    project_runs = [r for r in runs if r.get("project") == "p2p-register"]
    project_total = sum(r.get("cost_usd") or 0.0 for r in project_runs)
    shipped = sum(1 for p in (ROOT / "tasks").glob("*.md")
                  if re.search(r"^state:\s*shipped\s*$",
                               p.read_text(encoding="utf-8").split("---")[1], re.MULTILINE))
    mean_direct = sum(direct.values()) / len(direct) if direct else 0.0
    realised_per_shipped = project_total / shipped if shipped else 0.0
    direct_to_realised = realised_per_shipped / mean_direct if mean_direct else 1.0

    return {
        "bottom_up_to_direct": bottom_up_to_direct,
        "bottom_up_to_direct_n": len(ratios),
        "bottom_up_to_direct_spread": [round(ratios[0], 2), round(ratios[-1], 2)],
        "direct_to_realised": direct_to_realised,
        "combined": bottom_up_to_direct * direct_to_realised,
        "evidence": {
            "project_total_usd": round(project_total, 2),
            "project_runs": len(project_runs),
            "tasks_shipped": shipped,
            "realised_per_shipped_usd": round(realised_per_shipped, 2),
            "mean_direct_attributed_usd": round(mean_direct, 2),
            "attributed_tasks": len(direct),
        },
        "per_task": per_task,
        "_top_down_cross_check": (
            "The crude alternative is realised_per_shipped_usd times the number of items left, "
            "which assumes the items remaining are the same size as the ones shipped. Report it "
            "beside the corrected bottom-up figure: where the two disagree, the disagreement is "
            "the real uncertainty and averaging them would hide it."
        ),
    }


def fit(lam: float = 0.6) -> dict:
    turn_samples = read_turn_samples()
    cost_samples = read_cost_samples()
    model = {
        "_note": "DERIVED by estimate.py --fit. Never hand-edit: re-fit it. "
                 "Two stages kept apart because they have very different evidence behind them "
                 "-- words->turns is weak and carries the uncertainty, turns->cost is strong.",
        "fitted_at_note": "Re-fit whenever preflight-ledger.md gains a row or runs.json is "
                          "regenerated. A stale model still answers; it just answers from less.",
        "turn_model": fit_turn_model(turn_samples, lam),
        "cost_curve": fit_cost_curve(cost_samples),
        "turn_samples": turn_samples,
        "cost_samples_n": len(cost_samples),
        "_floor_rule": "Every cost here is a client-side estimate at list rates and a FLOOR on "
                       "what the account loses, never a bill and never sterling.",
    }
    model["realisation"] = fit_realisation(model)
    return model


def load_model(path: Path = MODEL_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- prediction

def predict_turns(text: str, model: dict) -> float:
    tm = model["turn_model"]
    full = dict(zip(TERMS, design_row(features(text))))
    names = ["const"] + tm["terms"]
    values = [1.0] + [full[t] for t in tm["terms"]]
    return math.exp(sum(v * tm["coefficients"][n] for v, n in zip(values, names)))


def ceiling() -> dict:
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    return {
        "gbp": gate["session_ceiling_gbp"],
        "messages": gate["session_ceiling_assistant_messages"],
    }


def cost_for_turns(turns: float, model: dict, quantile_key: str = "0.5") -> float:
    """Price a turn count, SPLIT ACROSS SESSIONS AT THE CEILING.

    This is not decoration. The ceiling is 90 assistant messages, so work needing 200 turns is
    three sessions and not one long one -- and three short sessions are CHEAPER than one long
    one, because cost per message rises with context, while each restart pays an orientation
    of about one session's worth of reading. Pricing 200 turns on the single-session curve
    would overstate it; ignoring the restarts would understate it.
    """
    curve = model["cost_curve"]
    a = curve["a_per_message"]
    b = curve["b_per_message_squared_over_100"]
    ratio = curve["ratio_quantiles"][quantile_key]
    cap = ceiling()["messages"]
    remaining = max(turns, 1.0)
    total = 0.0
    sessions = 0
    while remaining > 0.5:
        block = min(remaining, cap)
        total += a * block + b * block ** 2 / 100.0
        remaining -= block
        sessions += 1
    # Every session after the first re-reads the register. Measured at $1.20 (R-284aaeee, 17
    # messages) and written down in gate.json's own basis.
    total += 1.20 * (sessions - 1)
    return total * ratio


def sessions_for_turns(turns: float) -> int:
    return max(1, math.ceil(max(turns, 1.0) / ceiling()["messages"]))


def estimate(text: str, model: dict, apply_realisation: bool = True) -> dict:
    """A proposal in words goes in. A breakdown and a range come back.

    apply_realisation is False only while the realisation factor is being measured, which is
    the one place it would be circular.
    """
    items = split_items(text)
    per_item = []
    for item in items:
        turns = predict_turns(item, model)
        per_item.append({"item": item, "turns_p50": turns, "features": features(item)})

    total_turns = sum(i["turns_p50"] for i in per_item)
    # Doing several items in one proposal shares the reading, the orientation and the close,
    # so the whole is less than the sum of its parts. The discount is the same log-sublinearity
    # the turn model already carries, applied once across items rather than twice.
    if len(per_item) > 1:
        total_turns *= 1.0 / (1.0 + 0.06 * math.log(len(per_item)))

    res = model["turn_model"]["loo_log_residual_quantiles"]
    turns_low = total_turns * math.exp(res["0.1"])
    turns_high = total_turns * math.exp(res["0.9"])

    factor = 1.0
    if apply_realisation and model.get("realisation"):
        factor = model["realisation"]["combined"]

    out = {
        "proposal": text,
        "items": per_item,
        "turns": {
            "low": turns_low,
            "p50": total_turns,
            "high": turns_high,
        },
        "sessions": {
            "low": sessions_for_turns(turns_low),
            "p50": sessions_for_turns(total_turns),
            "high": sessions_for_turns(turns_high),
        },
        "realisation_factor": factor,
        "cost_usd": {
            "low": cost_for_turns(turns_low, model, "0.1") * factor,
            "p50": cost_for_turns(total_turns, model, "0.5") * factor,
            "high": cost_for_turns(turns_high, model, "0.9") * factor,
        },
        "hit_rate": model.get("backtest", {}).get("hit_rate"),
        "hit_rate_basis": model.get("backtest", {}).get("basis"),
        "floor_rule": "A client-side estimate at list rates and a floor on what the account "
                      "loses. Not a bill. Not sterling.",
    }
    # Allocate the whole to the parts so a breakdown adds up to the total it is shown beside.
    scale = out["cost_usd"]["p50"] / max(sum(i["turns_p50"] for i in per_item), 1e-9)
    for item in per_item:
        item["cost_usd_p50"] = item["turns_p50"] * scale
    return out


# ---------------------------------------------------------------- many proposals at once

def aggregate(proposals: list[dict], model: dict) -> dict:
    """Price a whole programme: many proposals, ONE number with a range.

    THIS IS THE PART THAT WORKS, and it works for a reason the per-item estimate cannot borrow.
    A single item's range is hopeless -- a factor of six, because the words do not say how big
    the work is. A SUM of many items is not, IF the errors are independent: the relative spread
    of a sum of n items falls with the square root of n, so sixteen items priced badly one by
    one still add up to a total worth deciding on.

    THE "IF" IS THE WHOLE ARGUMENT, SO BOTH ENDS ARE REPORTED AND NEITHER IS HIDDEN:

      * INDEPENDENT -- each item is its own draw. The tight interval.
      * SYSTEMATIC  -- one common error multiplies every item at once (a model price change, a
        way of working, one person's habit of taking on a second instruction mid-session). The
        interval is then exactly as wide as a single item's, and no amount of counting helps.

    The truth is in between and nothing here measures where, so the honest presentation is the
    pair. DECIDE ON THE SYSTEMATIC END: if the programme is unaffordable there, it is
    unaffordable, and the tight number is the reward for the errors being kind.
    """
    sd = model["turn_model"]["loo_log_residual_sd"]
    factor = model.get("realisation", {}).get("combined", 1.0)
    rows = []
    for proposal in proposals:
        text = proposal["text"]
        turns = predict_turns(text, model)
        rows.append({
            "id": proposal.get("id"),
            "source": proposal.get("source"),
            "text": text,
            "turns_p50": turns,
            "cost_usd_p50": cost_for_turns(turns, model, "0.5") * factor,
        })

    n = len(rows)
    total_turns = sum(r["turns_p50"] for r in rows)
    total_p50 = sum(r["cost_usd_p50"] for r in rows)

    cv_one = math.sqrt(math.exp(sd * sd) - 1.0)
    cv_independent = cv_one / math.sqrt(max(n, 1))

    def band(cv: float) -> dict:
        # Lognormal with the same coefficient of variation, so the band stays asymmetric and
        # the low end cannot go through zero.
        sigma = math.sqrt(math.log(1.0 + cv * cv))
        return {
            "low": total_p50 * math.exp(-1.2816 * sigma),
            "p50": total_p50,
            "high": total_p50 * math.exp(1.2816 * sigma),
        }

    realisation = model.get("realisation", {})
    per_shipped = realisation.get("evidence", {}).get("realised_per_shipped_usd")
    top_down = (per_shipped * n) if per_shipped else None

    return {
        "n_items": n,
        "total_turns_p50": total_turns,
        # The realisation factor is turns as much as money -- the overhead it measures is
        # handoffs, orientation and re-work, and those are sessions he sits through. Counting
        # sessions on the uncorrected turns would promise him half the interruptions.
        "total_turns_realised_p50": total_turns * (realisation.get("combined") or 1.0),
        "sessions_at_ceiling": math.ceil(
            total_turns * (realisation.get("combined") or 1.0) / ceiling()["messages"]),
        "ceiling": ceiling(),
        "realisation_factor": realisation.get("combined"),
        "top_down_cross_check_usd": top_down,
        "top_down_basis": (
            f"{n} items at ${per_shipped} realised per item shipped so far. Assumes what is "
            f"left is the same size as what is done, which is the assumption the bottom-up "
            f"figure exists to avoid." if per_shipped else None),
        "independent": band(cv_independent),
        "systematic": band(cv_one),
        "interval": "10th to 90th percentile",
        "per_item_cv": cv_one,
        "items": rows,
        "hit_rate": model.get("backtest", {}).get("hit_rate"),
        "floor_rule": "A client-side estimate at list rates and a floor on what the account "
                      "loses. Not a bill. Not sterling.",
    }


# ---------------------------------------------------------------- backtest

def backtest(model: dict) -> dict:
    """Leave-one-out over every row the model was fitted on, then over cost where it is known.

    THE BACKTEST IS THE DELIVERABLE, not the estimate. The question it answers is the only one
    worth asking of an estimator: when it says a range, how often is it right? An interval that
    contains the truth nine times in ten is worth reading; one that contains it half the time
    is worse than nothing, because it will be believed anyway.
    """
    samples = model["turn_samples"]
    tm = model["turn_model"]
    lam = tm["lambda"]
    rows = _rows_for(samples, tm["terms"])
    ys = [math.log(s["turns"]) for s in samples]
    res = tm["loo_log_residual_quantiles"]

    hits, ratios, details = 0, [], []
    for i, sample in enumerate(samples):
        coef = ridge_fit(rows[:i] + rows[i + 1:], ys[:i] + ys[i + 1:], lam)
        pred = math.exp(sum(x * y for x, y in zip(rows[i], coef)))
        low, high = pred * math.exp(res["0.1"]), pred * math.exp(res["0.9"])
        hit = low <= sample["turns"] <= high
        hits += 1 if hit else 0
        ratios.append(sample["turns"] / pred)
        details.append({
            "label": sample["label"][:70],
            "actual_turns": sample["turns"],
            "predicted_turns_p50": round(pred, 1),
            "range": [round(low, 1), round(high, 1)],
            "hit": hit,
        })

    ratios.sort()
    n = len(samples)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hits / n if n else None,
        "method": "leave-one-out: each row predicted by a model fitted without it, then "
                  "checked against the 10th-90th percentile interval.",
        "basis": f"{hits} of {n} leave-one-out predictions contained the actual turn count "
                 f"inside the stated range.",
        "interval_nominal": 0.8,
        "ratio_quantiles": {
            "0.1": round(quantile(ratios, 0.1), 3),
            "0.5": round(quantile(ratios, 0.5), 3),
            "0.9": round(quantile(ratios, 0.9), 3),
        },
        "worst_overrun": max(details, key=lambda d: d["actual_turns"] / max(d["predicted_turns_p50"], 1e-9)),
        "details": details,
    }


def baseline_from_ledger(path: Path = LEDGER_PATH) -> dict:
    """What the sessions' own hand-made estimates achieved, which is the bar to beat.

    Read from the ledger's est and actual columns where both carry a dollar figure. This is the
    honest comparator: not zero, because a session did state a range and sometimes got it right.
    """
    in_range = out_of_range = 0
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\d{4}-\d\d-\d\d \|", line):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 5:
            continue
        est_values = [float(x) for x in re.findall(r"\$([0-9]+(?:\.[0-9]{2})?)", cells[3])]
        act_values = [float(x) for x in re.findall(r"\$([0-9]+\.[0-9]{2})", cells[4])]
        if not est_values or not act_values:
            continue
        high = max(est_values)
        actual = act_values[0]
        ok = actual <= high
        in_range += 1 if ok else 0
        out_of_range += 0 if ok else 1
        rows.append({"label": cells[2][:60], "stated_high": high, "actual": actual, "within": ok})
    total = in_range + out_of_range
    return {
        "n": total,
        "within_stated_high": in_range,
        "hit_rate": in_range / total if total else None,
        "method": "the session's own stated p95 or hard budget against the measured actual, "
                  "one-sided: did the work come in at or under the number it announced.",
        "rows": rows,
    }


# ---------------------------------------------------------------- rendering

def render(est: dict) -> str:
    lines = []
    cost, turns, sessions = est["cost_usd"], est["turns"], est["sessions"]
    lines.append("PROPOSAL")
    lines.append("  " + (est["proposal"][:300] or "(empty)"))
    lines.append("")
    lines.append(f"BROKEN INTO {len(est['items'])} PIECE(S)")
    for item in est["items"]:
        lines.append(f"  ${item['cost_usd_p50']:6.2f}  {item['turns_p50']:5.1f} turns  "
                     f"{item['item'][:88]}")
    lines.append("")
    lines.append("COST, AS A RANGE AND NEVER A POINT")
    lines.append(f"  low   ${cost['low']:8.2f}   ({turns['low']:.0f} turns, "
                 f"{sessions['low']} session(s))")
    lines.append(f"  p50   ${cost['p50']:8.2f}   ({turns['p50']:.0f} turns, "
                 f"{sessions['p50']} session(s))")
    lines.append(f"  high  ${cost['high']:8.2f}   ({turns['high']:.0f} turns, "
                 f"{sessions['high']} session(s))")
    if est.get("hit_rate") is not None:
        lines.append("")
        lines.append(f"HIT RATE OF THIS KIND OF RANGE: {est['hit_rate']*100:.0f}%")
        lines.append(f"  {est['hit_rate_basis']}")
    lines.append("")
    lines.append("  " + est["floor_rule"])
    return "\n".join(lines)


def render_programme(agg: dict) -> str:
    lines = [f"{agg['n_items']} PIECES OF WORK, PRICED AS ONE PROGRAMME", ""]
    for item in agg["items"]:
        tag = (item["id"] or "")[:8]
        lines.append(f"  ${item['cost_usd_p50']:6.2f}  {item['turns_p50']:5.1f} turns  "
                     f"{tag:8s} {item['text'][:78]}")
    lines += [
        "",
        f"  {agg['total_turns_realised_p50']:.0f} assistant messages in total, which is "
        f"{agg['sessions_at_ceiling']} sessions at the "
        f"{agg['ceiling']['messages']}-message ceiling",
        "",
        "THE TOTAL, AS A RANGE (10th-90th percentile)",
        f"  if the errors are independent   ${agg['independent']['low']:8.2f}  to  "
        f"${agg['independent']['high']:8.2f}   (p50 ${agg['independent']['p50']:.2f})",
        f"  if the error is systematic      ${agg['systematic']['low']:8.2f}  to  "
        f"${agg['systematic']['high']:8.2f}   (p50 ${agg['systematic']['p50']:.2f})",
        "",
        "  Decide on the systematic end. The tight one is the reward for the errors being kind,",
        "  and nothing here measures which of the two this programme is.",
    ]
    if agg.get("top_down_cross_check_usd"):
        lines += [
            "",
            f"CROSS-CHECK, THE CRUDE WAY:  ${agg['top_down_cross_check_usd']:.2f}",
            f"  {agg['top_down_basis']}",
            "  Where this and the bottom-up p50 disagree, the disagreement IS the uncertainty.",
        ]
    lines += [
        f"",
        f"  Both include a realisation factor of "
        f"{agg.get('realisation_factor', 1.0):.2f}x, measured and not assumed: a brief costs "
        f"more than the median row it looks like, and a shipped item costs more than the session "
        f"that shipped it.",
        "",
        "  " + agg["floor_rule"],
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proposal", help="the proposal, in words")
    parser.add_argument("--file", help="read the proposal from a file")
    parser.add_argument("--fit", action="store_true", help="re-derive the model from the ledger")
    parser.add_argument("--write-model", action="store_true",
                        help="with --fit, write estimator/model.json")
    parser.add_argument("--backtest", action="store_true",
                        help="leave-one-out hit rate, and the hand-estimate baseline it beats")
    parser.add_argument("--programme", metavar="JSON",
                        help="price many proposals as one total: a JSON list of "
                             "{id, text} objects, or a text file of one proposal per line")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--lam", type=float, default=0.6, help="ridge penalty (default 0.6)")
    args = parser.parse_args(argv)

    if args.fit:
        model = fit(args.lam)
        model["backtest"] = backtest(model)
        model["baseline_hand_estimates"] = baseline_from_ledger()
        if args.write_model:
            MODEL_PATH.write_text(json.dumps(model, indent=1) + "\n", encoding="utf-8")
            print(f"-> {MODEL_PATH}")
        bt = model["backtest"]
        print(f"turn model: n={model['turn_model']['n']} "
              f"loo median ratio error {model['turn_model']['loo_median_abs_ratio_error']*100:.0f}%")
        print(f"cost curve: n={model['cost_curve']['n']}")
        print(f"backtest:   {bt['hits']}/{bt['n']} inside range = {bt['hit_rate']*100:.0f}% "
              f"(nominal 80%)")
        base = model["baseline_hand_estimates"]
        print(f"baseline:   {base['within_stated_high']}/{base['n']} hand estimates held = "
              f"{base['hit_rate']*100:.0f}%")
        return 0

    model = load_model()

    if args.backtest:
        bt = model.get("backtest") or backtest(model)
        if args.json:
            print(json.dumps(bt, indent=1))
            return 0
        print(f"LEAVE-ONE-OUT BACKTEST: {bt['hits']}/{bt['n']} = {bt['hit_rate']*100:.0f}% "
              f"inside the stated range (nominal {bt['interval_nominal']*100:.0f}%)")
        print(f"  {bt['method']}")
        print()
        for d in bt["details"]:
            mark = "hit " if d["hit"] else "MISS"
            print(f"  {mark} actual {d['actual_turns']:4d}  p50 {d['predicted_turns_p50']:6.1f}  "
                  f"range {d['range'][0]:5.1f}-{d['range'][1]:6.1f}  {d['label']}")
        base = model.get("baseline_hand_estimates")
        if base:
            print()
            print(f"BASELINE, the hand-written estimates this has to beat: "
                  f"{base['within_stated_high']}/{base['n']} = {base['hit_rate']*100:.0f}% came "
                  f"in at or under the number the session announced.")
        return 0

    if args.programme:
        raw = Path(args.programme).read_text(encoding="utf-8")
        try:
            proposals = json.loads(raw)
            if isinstance(proposals, dict):
                proposals = proposals.get("proposals", [])
        except json.JSONDecodeError:
            proposals = [{"id": None, "text": line.strip()}
                         for line in raw.splitlines() if line.strip()]
        agg = aggregate(proposals, model)
        if args.json:
            print(json.dumps(agg, indent=1))
            return 0
        print(render_programme(agg))
        return 0

    text = args.proposal
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text:
        parser.error("give me a proposal: --proposal or --file")

    est = estimate(text, model)
    print(json.dumps(est, indent=1) if args.json else render(est))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
