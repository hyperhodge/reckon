#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 limits.py [--window five_hour|seven_day] [--out FILE]

Answers "will I get through this week?" -- spec 5.4's first question -- from
evidence rather than theory.

There is no API for subscription headroom; a session cannot read its own
remaining allowance. But Claude Code records the moment the door shuts: a
rejected request carries a quotaLimits block naming the window type and when it
resets. That is a calibration point. Pair it with the token volumes already in
the transcripts and the ceiling can be estimated from your own history, which is
what 5.5 asks for -- estimate from history, never from theory.

Every ceiling here is OBSERVED, not published. It says "you have been refused at
about this much before", not "your limit is this". The distinction matters: the
metering unit is unknown, cache reads may not count one-for-one against fresh
input, and one observation is a marker rather than a denominator.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aggregate as ag

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "limits.json"
OBSERVATIONS_PATH = HERE / "observations.json"

# Window lengths keyed by the rateLimitType Claude Code reports.
WINDOWS = {
    "five_hour": timedelta(hours=5),
    "seven_day": timedelta(days=7),
}
DEFAULT_WINDOW = timedelta(hours=5)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_observations():
    """Limit states seen in the UI. The transcript records refusals only; the app
    also warns before it refuses, and a warning is the cheaper calibration point
    because nothing had to fail to produce it."""
    if not OBSERVATIONS_PATH.exists():
        return []
    try:
        raw = json.loads(OBSERVATIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out = []
    for entry in raw.get("observations", []):
        out.append({
            "at": entry.get("at"), "type": entry.get("type") or "unknown",
            "status": entry.get("state"), "overage_status": None,
            "overage_disabled_reason": None, "using_overage": None,
            "resets_at": entry.get("resets_at"), "project": "(reported)",
            "source": "observed in UI", "note": entry.get("note"),
        })
    return out


def scan(projects_dir, prices):
    """Return (messages, events). Messages are (when, counts, cost). Events are
    every quotaLimits block the transcripts carry."""
    messages, events, seen = [], [], set()
    for record, path in ag.iter_transcripts(projects_dir):
        if record is None:
            continue
        when = parse_ts(record.get("timestamp"))

        quota = record.get("quotaLimits")
        if quota and when:
            resets = quota.get("resetsAt")
            events.append({
                "at": when.isoformat(),
                "type": quota.get("rateLimitType") or "unknown",
                "status": quota.get("status"),
                "overage_status": quota.get("overageStatus"),
                "overage_disabled_reason": quota.get("overageDisabledReason"),
                "using_overage": quota.get("isUsingOverage"),
                "resets_at": (datetime.fromtimestamp(resets, timezone.utc).isoformat()
                              if isinstance(resets, (int, float)) else None),
                "project": ag.project_name(record, path),
            })

        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        model = message.get("model")
        if not model or not usage or not when:
            continue
        msg_id = message.get("id")
        if msg_id:
            if msg_id in seen:
                continue
            seen.add(msg_id)
        counts = ag.usage_counts(usage)
        price = ag.price_for(model, prices)
        cost = sum(ag.message_cost(counts, price).values()) if price else 0.0
        messages.append((when, counts, cost))

    events.extend(load_observations())
    messages.sort(key=lambda m: m[0])
    events.sort(key=lambda e: e["at"] or "")
    return messages, events


def window_start(end, window, resets):
    """Where the rolling window actually begins.

    A rolling window is not simply `end - window`: once the service resets one,
    everything before the reset stops counting. Ignoring that is how a tracker
    reports 210% of a ceiling while nothing is being refused. Use the most recent
    reset at or before `end` when it is later than the naive start.
    """
    naive = end - window
    passed = [r for r in resets if r and r <= end]
    return max([naive] + passed) if passed else naive


def consumption(messages, end, window, resets=()):
    """Token volumes in the window ending at `end`, honouring any reset."""
    start = window_start(end, window, resets)
    total = defaultdict(int)
    total["cost"] = 0.0
    for when, counts, cost in messages:
        if start <= when <= end:
            for key, value in counts.items():
                total[key] += value
            total["cost"] += cost
            total["messages"] += 1
    total["all_classes"] = (total["input"] + total["output"]
                            + total["cache_write_5m"] + total["cache_write_1h"]
                            + total["cache_read"])
    total["window_start"] = start.isoformat()
    return dict(total)


def calibrate(messages, events):
    """One observation per refusal: what had been consumed when it happened."""
    points = []
    for event in events:
        if event["status"] != "rejected":
            continue
        at = parse_ts(event["at"])
        window = WINDOWS.get(event["type"], DEFAULT_WINDOW)
        # Earlier resets of the same window type bound how far back to look.
        prior = [parse_ts(e["resets_at"]) for e in events
                 if e["type"] == event["type"] and parse_ts(e["resets_at"])
                 and parse_ts(e["resets_at"]) <= at]
        used = consumption(messages, at, window, prior)
        points.append({
            "at": event["at"], "type": event["type"], "project": event["project"],
            "window_hours": round(window.total_seconds() / 3600, 2),
            "consumed": used,
            "overage_would_have_charged": event["overage_status"] == "rejected"
                                          and not event["using_overage"],
        })
    return points


def ceilings(points):
    """Observed ceiling per window type. The lowest refusal is the safest
    estimate: you were certainly refused there."""
    out = {}
    grouped = defaultdict(list)
    for p in points:
        grouped[p["type"]].append(p["consumed"]["all_classes"])
    for window_type, values in grouped.items():
        out[window_type] = {
            "observations": len(values),
            "lowest_refusal_tokens": min(values),
            "highest_refusal_tokens": max(values),
            "basis": ("one observation -- a marker, not a denominator"
                      if len(values) == 1 else
                      f"{len(values)} observations; treat the lowest as the working ceiling"),
        }
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--window", choices=sorted(WINDOWS), default="five_hour")
    parser.add_argument("--projects", default=None)
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args(argv)

    try:
        prices = ag.load_prices()
    except ag.PriceTableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.projects:
        projects_dir, source_label = Path(args.projects), "explicit --projects"
    else:
        projects_dir, source_label = ag.default_source()
    if not projects_dir.exists():
        print(f"ERROR: no transcript directory at {projects_dir}", file=sys.stderr)
        return 2

    messages, events = scan(projects_dir, prices)
    points = calibrate(messages, events)
    observed = ceilings(points)

    now = messages[-1][0] if messages else datetime.now(timezone.utc)
    window = WINDOWS[args.window]
    resets = [parse_ts(e["resets_at"]) for e in events if e["type"] == args.window]
    current = consumption(messages, now, window, [r for r in resets if r])

    by_month = defaultdict(lambda: defaultdict(float))
    for when, counts, cost in messages:
        key = when.strftime("%Y-%m")
        by_month[key]["cost"] += cost
        by_month[key]["messages"] += 1
        for name, value in counts.items():
            by_month[key][name] += value

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(projects_dir), "source_kind": source_label,
        "basis": ("Observed, not published. Subscription headroom cannot be read from "
                  "inside a session; these are the points at which requests were "
                  "actually refused, and the volumes measured at those moments."),
        "window": args.window,
        "current_window": current,
        "observed_ceilings": observed,
        "calibration_points": points,
        "limit_events": events,
        "by_month": {k: {kk: (round(vv, 6) if kk == "cost" else int(vv))
                         for kk, vv in v.items()} for k, v in sorted(by_month.items())},
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _report(payload, args.window)
    return 0


def _report(payload, window):
    current = payload["current_window"]
    observed = payload["observed_ceilings"].get(window)
    print()
    print(f"Source: {payload['source_kind']} — {payload['source']}")
    print(f"\nCurrent {window.replace('_', '-')} window "
          f"(from {current['window_start'][11:19]} to the last recorded message):")
    print(f"  messages    {int(current.get('messages', 0)):>12,}")
    print(f"  output      {current['output']:>12,}")
    print(f"  cache write {current['cache_write_5m'] + current['cache_write_1h']:>12,}")
    print(f"  cache read  {current['cache_read']:>12,}")
    print(f"  all classes {current['all_classes']:>12,}   (${current['cost']:,.2f} at list)")

    if observed:
        ceiling = observed["lowest_refusal_tokens"]
        pct = current["all_classes"] / ceiling * 100 if ceiling else 0
        print(f"\nAgainst the observed ceiling ({observed['observations']} refusal"
              f"{'s' if observed['observations'] != 1 else ''} recorded):")
        print(f"  refused before at {ceiling:,} tokens in this window")
        print(f"  now at            {current['all_classes']:,}  =  {pct:.0f}% of that")
        print(f"  {observed['basis']}")
        if pct > 100:
            print("  NOTE: past the observed ceiling without a refusal, so summed "
                  "tokens are not what the limit meters — cache reads almost "
                  "certainly count for less than fresh input. Treat the percentage "
                  "as a shape, not a gauge, until more refusals calibrate it.")
    else:
        print(f"\nNo refusal recorded yet for the {window} window, so there is no "
              f"observed ceiling to compare against. Nothing to report until one happens.")

    if payload["limit_events"]:
        print(f"\nLimit events on record ({len(payload['limit_events'])}):")
        for e in payload["limit_events"]:
            note = ""
            if e.get("source"):
                note = f"  <- {e['source']}"
            elif e["overage_status"] == "rejected" and not e["using_overage"]:
                note = "  <- overage offered and refused; auto-recharge is off"
            print(f"  {e['at'][:19]}  {e['type']:<10} {e['status']:<9} "
                  f"resets {(e['resets_at'] or '?')[11:19]}{note}")

    print("\nBy month (all token classes, list-rate cost):")
    print(f"  {'month':<9}{'messages':>10}{'tokens':>16}{'$':>10}")
    for month, m in payload["by_month"].items():
        tokens = (m["input"] + m["output"] + m["cache_write_5m"]
                  + m["cache_write_1h"] + m["cache_read"])
        print(f"  {month:<9}{m['messages']:>10,}{tokens:>16,}{m['cost']:>10.2f}")
    print(f"\n-> {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
