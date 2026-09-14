#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 aggregate.py [--with-illustrative] [--projects DIR] [--out FILE]

Walks ~/.claude/projects/**/*.jsonl, prices token usage from prices.json, and
writes usage.json next to this script. Read-only with respect to the transcript
tree. No third-party dependencies, no network.

Extends llm-monitor/aggregate.py (July 2026) per P-0001-T01:
  - prices live in prices.json, not in this file
  - synthetic multi-vendor rows are opt-in and never share a total with real cost
  - every row carries a rail, and the number of assumed rails is reported
  - the input side is compared against output, per model and per project
"""

import argparse
import json
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRICES_PATH = HERE / "prices.json"
RAILS_PATH = HERE / "rails.json"
ARCHIVE_CONFIG_PATH = HERE / "archive-config.json"
OUT_PATH = HERE / "usage.json"

DATE_SUFFIX = re.compile(r"-\d{8}$")
MILLION = 1_000_000

# Illustrative only. Other vendors that leave no per-token record on this
# machine, so they cannot be measured, only asserted. Off unless asked for, and
# never added into a real total -- see totals.real vs totals.illustrative.
SHADOW_AI_DAILY_USD = {
    "gpt-4o": (2.0, 6.5),
    "gpt-4o-mini": (0.2, 0.9),
    "gemini-1.5-pro": (1.0, 3.0),
    "github-copilot": (1.2, 1.8),
}

TOKEN_CLASSES = ("input", "output", "cache_write", "cache_read")


# ---------------------------------------------------------------- prices ----

class PriceTableError(Exception):
    """prices.json is missing or unusable. Never fall back to zero."""


def load_prices(path=PRICES_PATH):
    """Load the price table. A missing or broken table is fatal: costing
    everything at zero is a worse answer than refusing to answer."""
    if not path.exists():
        raise PriceTableError(
            f"No price table at {path}.\n"
            f"Without it every row would cost $0.00, which reads like a real "
            f"result and is not one. Restore the file (see TASK-001-ledger.md "
            f"for the table) and run again."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PriceTableError(f"{path} is not valid JSON: {exc}") from exc

    models = raw.get("models")
    if not isinstance(models, dict) or not models:
        raise PriceTableError(f"{path} has no usable 'models' table.")
    required = {"input", "output", "cache_write_5m", "cache_read"}
    for family, row in models.items():
        missing = required - set(row or {})
        if missing:
            raise PriceTableError(
                f"{path}: model '{family}' is missing {sorted(missing)}.")
    return raw


def price_for(model, table):
    """Price row for a model, date suffix stripped (-20250929 and friends)."""
    models = table["models"]
    family = DATE_SUFFIX.sub("", model or "")
    return models.get(family) or models.get(model or "")


def message_cost(counts, price):
    """USD cost of one message's four token classes, as a dict per class.

    1-hour cache writes bill above the 5-minute rate. Where the table gives no
    1h rate they are priced at the 5m rate and the volume is reported, so the
    understatement is visible instead of silent.
    """
    rate_5m = price["cache_write_5m"]
    rate_1h = price.get("cache_write_1h")
    rate_1h = rate_5m if rate_1h is None else rate_1h
    return {
        "input": counts["input"] * price["input"] / MILLION,
        "output": counts["output"] * price["output"] / MILLION,
        "cache_write": (counts["cache_write_5m"] * rate_5m
                        + counts["cache_write_1h"] * rate_1h) / MILLION,
        "cache_read": counts["cache_read"] * price["cache_read"] / MILLION,
    }


# ----------------------------------------------------------------- rails ----

def load_rails(path=RAILS_PATH):
    """Rail overrides. Absent is fine: everything is then the default."""
    if not path.exists():
        return {"default": "subscription", "rules": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PriceTableError(f"{path} is not valid JSON: {exc}") from exc
    raw.setdefault("default", "subscription")
    raw.setdefault("rules", [])
    return raw


def rail_for(project, date, rails):
    """Return (rail, used_default). First matching rule wins."""
    for rule in rails["rules"]:
        if "project" in rule and rule["project"] != project:
            continue
        if "from" in rule and date < rule["from"]:
            continue
        if "to" in rule and date > rule["to"]:
            continue
        return rule.get("rail", "api"), False
    return rails["default"], True


# ------------------------------------------------------------ transcripts ----

def default_source():
    """Prefer the archive over the live tree.

    ~/.claude/projects is pruned by Claude Code's own cleanup, so reading it
    directly means the ledger silently loses history it once had. archive.py
    keeps a durable copy; when one exists it is the better source, because it is
    a superset. Returns (path, label).
    """
    if ARCHIVE_CONFIG_PATH.exists():
        try:
            raw = json.loads(ARCHIVE_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
        archive = Path(raw.get("archive", "")).expanduser()
        if archive and archive.exists() and any(archive.glob("*/*.jsonl")):
            return archive, "archive"
    return Path.home() / ".claude" / "projects", "live transcripts"


def project_name(record, jsonl_path):
    cwd = record.get("cwd")
    if cwd:
        return Path(cwd).name
    # Fallback: best-effort, lossy if the project name itself has hyphens.
    return jsonl_path.parent.name.lstrip("-").split("-")[-1]


def iter_transcripts(projects_dir):
    """Yield (record, path) for every parseable line. Unparseable lines are
    yielded as (None, path) so the caller can count them rather than lose them."""
    for jsonl_path in sorted(Path(projects_dir).glob("*/*.jsonl")):
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line), jsonl_path
                except json.JSONDecodeError:
                    yield None, jsonl_path


def usage_counts(usage):
    """The four token classes, with the cache write split by TTL."""
    creation = usage.get("cache_creation") or {}
    total_write = usage.get("cache_creation_input_tokens", 0) or 0
    w5 = creation.get("ephemeral_5m_input_tokens")
    w1 = creation.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        # Older transcripts carry no breakdown. Treat as 5m, which is what the
        # price table is quoted at.
        w5, w1 = total_write, 0
    else:
        w5, w1 = w5 or 0, w1 or 0
    return {
        "input": usage.get("input_tokens", 0) or 0,
        "output": usage.get("output_tokens", 0) or 0,
        "cache_write_5m": w5,
        "cache_write_1h": w1,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
    }


def new_bucket():
    return {
        "input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0,
        "cache_read": 0,
        "cost": {k: 0.0 for k in TOKEN_CLASSES},
        "messages": 0,
        "entrypoints": set(),
    }


def collect(records, prices, rails):
    """Fold transcript records into (date, project, model) buckets.

    Dedupes on message.id: Claude Code writes one line per content block, and
    the same assistant message repeats its usage on each. Counting a message
    twice inflates every figure downstream, so this is the load-bearing line.
    """
    buckets = defaultdict(new_bucket)
    seen_msg_ids = set()
    stats = {
        "parse_errors": 0, "assistant_messages": 0, "duplicate_lines": 0,
        "cache_write_1h_tokens_priced_at_5m": 0, "dates": set(),
        "entrypoints": defaultdict(int),
    }
    unpriced = set()
    ignored = set(prices.get("ignored_models", []))

    for record, jsonl_path in records:
        if record is None:
            stats["parse_errors"] += 1
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        model, msg_id = message.get("model"), message.get("id")
        if not model or not usage:
            continue
        if msg_id:
            if msg_id in seen_msg_ids:
                stats["duplicate_lines"] += 1
                continue
            seen_msg_ids.add(msg_id)
        date = (record.get("timestamp") or "")[:10]
        if not date:
            continue
        if model in ignored:
            continue

        stats["assistant_messages"] += 1
        stats["dates"].add(date)
        project = project_name(record, jsonl_path)
        counts = usage_counts(usage)

        price = price_for(model, prices)
        if price is None:
            unpriced.add(model)
            costs = {k: 0.0 for k in TOKEN_CLASSES}
        else:
            costs = message_cost(counts, price)
            if price.get("cache_write_1h") is None:
                stats["cache_write_1h_tokens_priced_at_5m"] += counts["cache_write_1h"]

        bucket = buckets[(date, project, model)]
        for key in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
            bucket[key] += counts[key]
        for key in TOKEN_CLASSES:
            bucket["cost"][key] += costs[key]
        bucket["messages"] += 1
        # How the session was launched. Not proof of a rail, but the strongest
        # signal the transcript carries: a --bare or scripted -p run does not
        # look like this, and it is the case that silently bills API credit.
        entrypoint = record.get("entrypoint")
        if entrypoint:
            bucket["entrypoints"].add(entrypoint)
            stats["entrypoints"][entrypoint] += 1

    stats["dates"] = sorted(stats["dates"])
    stats["entrypoints"] = dict(sorted(stats["entrypoints"].items()))
    return buckets, sorted(unpriced), stats


def buckets_to_rows(buckets, rails):
    """Flatten buckets into output rows, stamping the rail on each."""
    rows, defaulted = [], 0
    for (date, project, model), b in sorted(buckets.items()):
        rail, used_default = rail_for(project, date, rails)
        defaulted += used_default
        rows.append({
            "date": date, "project": project, "model": model, "rail": rail,
            "rail_assumed": used_default,
            "input_tokens": b["input"],
            "output_tokens": b["output"],
            "cache_creation_tokens": b["cache_write_5m"] + b["cache_write_1h"],
            "cache_creation_5m_tokens": b["cache_write_5m"],
            "cache_creation_1h_tokens": b["cache_write_1h"],
            "cache_read_tokens": b["cache_read"],
            "messages": b["messages"],
            "entrypoints": sorted(b["entrypoints"]),
            "cost": round(sum(b["cost"].values()), 6),
            "cost_breakdown": {k: round(v, 6) for k, v in b["cost"].items()},
            "synthetic": False,
        })
    return rows, defaulted


# -------------------------------------------------------------- analysis ----

def _comparison(cost):
    """The §4 question, for one slice: is the input side really the small half?"""
    cache_read, output = cost["cache_read"], cost["output"]
    input_side = cost["input"] + cost["cache_write"] + cost["cache_read"]
    total = input_side + output
    return {
        "cache_read_cost": round(cache_read, 6),
        "output_cost": round(output, 6),
        "cache_read_to_output_ratio": round(cache_read / output, 4) if output else None,
        "input_side_cost": round(input_side, 6),
        "cache_read_share_of_input_side": round(cache_read / input_side, 4) if input_side else None,
        "input_side_share_of_total": round(input_side / total, 4) if total else None,
        "total_cost": round(total, 6),
    }


def build_analysis(buckets):
    by_model, by_project = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    overall = defaultdict(float)
    tokens_by_model = defaultdict(lambda: defaultdict(int))
    for (_date, project, model), b in buckets.items():
        for key, value in b["cost"].items():
            by_model[model][key] += value
            by_project[project][key] += value
            overall[key] += value
        for key in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
            tokens_by_model[model][key] += b[key]

    return {
        "question": ("Does the input side -- fresh input plus cache writes plus cache "
                     "reads -- cost more than output? Computed from the data, not assumed."),
        "overall": _comparison(overall),
        "cost_split": {k: round(v, 6) for k, v in overall.items()},
        "by_model": {m: _comparison(c) for m, c in sorted(by_model.items())},
        "by_project": {p: _comparison(c) for p, c in sorted(by_project.items())},
        "tokens_by_model": {m: dict(t) for m, t in sorted(tokens_by_model.items())},
    }


# ----------------------------------------------------------- illustrative ----

def shadow_ai_rows(project, rails):
    """Fabricated multi-vendor rows. Opt-in, tagged, and never summed into
    totals.real. They exist to show what multi-vendor sprawl would look like,
    not to claim it was measured."""
    rng = random.Random(42)
    rows = []
    for i in range(13, -1, -1):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).date()
        if date.weekday() >= 5:
            continue
        for model, (lo, hi) in SHADOW_AI_DAILY_USD.items():
            rail, _ = rail_for(project, date.isoformat(), rails)
            rows.append({
                "date": date.isoformat(), "project": project, "model": model,
                "rail": rail, "rail_assumed": True,
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
                "cache_creation_1h_tokens": 0, "cache_read_tokens": 0,
                "messages": 0,
                "entrypoints": [],
                "cost": round(rng.uniform(lo, hi), 4),
                "cost_breakdown": None,
                "synthetic": True,
            })
    return rows


# ------------------------------------------------------------------ main ----

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--with-illustrative", action="store_true",
                        help="include fabricated multi-vendor rows, reported "
                             "separately and never added to the real total")
    parser.add_argument("--projects", default=None,
                        help="transcript root (read-only); defaults to the archive "
                             "when archive-config.json points at a populated one, "
                             "otherwise ~/.claude/projects")
    parser.add_argument("--out", default=str(OUT_PATH), help="output JSON path")
    args = parser.parse_args(argv)

    try:
        prices = load_prices()
        rails = load_rails()
    except PriceTableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.projects:
        projects_dir, source_label = Path(args.projects), "explicit --projects"
    else:
        projects_dir, source_label = default_source()
    if not projects_dir.exists():
        print(f"ERROR: no transcript directory at {projects_dir}", file=sys.stderr)
        return 2

    buckets, unpriced, stats = collect(iter_transcripts(projects_dir), prices, rails)
    rows, rail_defaulted = buckets_to_rows(buckets, rails)
    real_total = sum(r["cost"] for r in rows)
    analysis = build_analysis(buckets)

    illustrative_rows, illustrative_total = [], 0.0
    if args.with_illustrative:
        anchor = rows[0]["project"] if rows else "workspace"
        illustrative_rows = shadow_ai_rows(anchor, rails)
        illustrative_total = sum(r["cost"] for r in illustrative_rows)

    days = sorted(rows + illustrative_rows,
                  key=lambda d: (d["date"], d["project"], d["model"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(projects_dir),
        "source_kind": source_label,
        "price_table": {"source": prices.get("source"),
                        "checked_on": prices.get("checked_on"),
                        "basis": "client-side estimate at list rates, not an invoice"},
        "days": days,
        "totals": {
            "real": {"cost_usd": round(real_total, 6), "rows": len(rows),
                     "days_covered": len(stats["dates"]),
                     "date_range": [stats["dates"][0], stats["dates"][-1]] if stats["dates"] else None,
                     "assistant_messages": stats["assistant_messages"]},
            "illustrative": {"cost_usd": round(illustrative_total, 6),
                             "rows": len(illustrative_rows),
                             "included": args.with_illustrative,
                             "basis": "fabricated, for display only; never added to real"},
        },
        "unpriced_models": unpriced,
        "rail_defaulted_rows": rail_defaulted,
        "rail_default": rails["default"],
        "entrypoints_observed": stats["entrypoints"],
        "parse_errors": stats["parse_errors"],
        "duplicate_lines_skipped": stats["duplicate_lines"],
        "cache_write_1h_tokens_priced_at_5m": stats["cache_write_1h_tokens_priced_at_5m"],
        "analysis": analysis,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    _print_summary(payload, analysis, stats, unpriced, rail_defaulted,
                   len(rows), out_path)
    return 0


def _print_summary(payload, analysis, stats, unpriced, rail_defaulted, n_rows, out_path):
    o = analysis["overall"]
    real = payload["totals"]["real"]
    ratio = o["cache_read_to_output_ratio"]
    ratio_text = f"{ratio:.2f}x" if ratio is not None else "n/a"

    print()
    print(f"Source: {payload['source_kind']} — {payload['source']}")
    print(f"{real['days_covered']} day(s) covered, ${real['cost_usd']:,.2f} real cost, "
          f"cache reads {ratio_text} the cost of output.")
    if real["date_range"]:
        print(f"Range {real['date_range'][0]} to {real['date_range'][1]}, "
              f"{real['assistant_messages']:,} assistant messages, {n_rows} rows.")
    print()

    split = analysis["cost_split"]
    print("Cost split (USD, list rates, client-side estimate):")
    for label, key in (("fresh input", "input"), ("cache write", "cache_write"),
                       ("cache read", "cache_read"), ("output", "output")):
        share = split[key] / o["total_cost"] * 100 if o["total_cost"] else 0
        print(f"  {label:<13} ${split[key]:>9.2f}   {share:>4.0f}%")
    print(f"  {'TOTAL':<13} ${o['total_cost']:>9.2f}")
    print()
    if o["input_side_share_of_total"] is not None:
        print(f"Input side is {o['input_side_share_of_total']*100:.0f}% of spend; "
              f"cache reads are {o['cache_read_share_of_input_side']*100:.0f}% of that.")
        verdict = ("the input side beats output"
                   if o["input_side_cost"] > o["output_cost"] else "output still dominates")
        print(f"Verdict: {verdict}.")
    print()

    print(f"{'model':<20}{'c-read $':>10}{'output $':>10}{'ratio':>9}{'in-side %':>11}")
    print("-" * 60)
    for model, c in sorted(analysis["by_model"].items(),
                           key=lambda kv: -kv[1]["cache_read_cost"]):
        r = f"{c['cache_read_to_output_ratio']:.2f}" if c["cache_read_to_output_ratio"] is not None else "-"
        s = f"{c['input_side_share_of_total']*100:.0f}%" if c["input_side_share_of_total"] is not None else "-"
        print(f"{model:<20}{c['cache_read_cost']:>10.2f}{c['output_cost']:>10.2f}{r:>9}{s:>11}")
    print()
    print(f"{'project':<30}{'c-read $':>10}{'output $':>10}{'ratio':>9}")
    print("-" * 59)
    for project, c in sorted(analysis["by_project"].items(),
                             key=lambda kv: -kv[1]["total_cost"]):
        r = f"{c['cache_read_to_output_ratio']:.2f}" if c["cache_read_to_output_ratio"] is not None else "-"
        print(f"{project:<30}{c['cache_read_cost']:>10.2f}{c['output_cost']:>10.2f}{r:>9}")
    print()

    if unpriced:
        print(f"WARNING: {len(unpriced)} unpriced model(s) contributed ZERO cost, "
              f"not an estimate: {', '.join(unpriced)}")
        print(f"         Add them to prices.json or the total below is understated.")
    stray_1h = payload["cache_write_1h_tokens_priced_at_5m"]
    if stray_1h:
        print(f"WARNING: {stray_1h:,} 1-hour cache-write tokens priced at the "
              f"5-minute rate because prices.json has no 1h rate for their model.")
        print(f"         1h writes bill above 5m, so cache-write cost here is a floor, "
              f"not a figure. See cache_write_1h_note in prices.json.")
    if stats["parse_errors"]:
        print(f"WARNING: {stats['parse_errors']:,} unparseable line(s) skipped; "
              f"the total is short by whatever they held.")
    if payload["totals"]["illustrative"]["included"]:
        print(f"NOTE: ${payload['totals']['illustrative']['cost_usd']:,.2f} of "
              f"illustrative rows written and kept OUT of the real total.")
    entrypoints = payload["entrypoints_observed"]
    if entrypoints:
        print(f"NOTE: launched via {', '.join(f'{k} ({v:,})' for k, v in entrypoints.items())}.")
    print(f"NOTE: {rail_defaulted} of {n_rows} rows fall through to rail "
          f"'{payload['rail_default']}' with no matching rule in rails.json.")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    sys.exit(main())
