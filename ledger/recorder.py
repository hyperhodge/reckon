#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 recorder.py [--source DIR] [--out FILE]
          python3 recorder.py --session <claude_session_id>
          python3 recorder.py --priors [--write-priors]

Reads archived Claude Code transcripts and writes Run and Output records
(spec-v0.2.md §2.3 and §2.4) to runs.json next to this script.

Why it exists: usage.json is keyed by (date, project, model), so on 5 September
the p2p-register row was $24.22 across three separate sessions and said nothing
about any one of them. A Run is one session id, priced on its own.

Parsing and pricing are aggregate.py's, imported rather than repeated. This
module adds the grouping, the output discovery and the joins.

THE RULE THAT SHAPES THIS FILE (§7): discovery reads the record, never the disk.
Claude Code already states which files it wrote -- every Write/Edit tool use in
the transcript carries its path -- and git commits are visible in the Bash tool
results that made them. There is no mtime comparison anywhere in this module,
and a test asserts that. Timestamp scanning picks up editor saves, formatter
runs, build artefacts and package installs, and misses nothing only by including
everything.

No third-party dependencies, no network. The transcript tree is read-only.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import aggregate  # noqa: E402  -- sibling module, parsing and pricing live there

RUN_TASKS_PATH = HERE / "run-tasks.json"
OUT_PATH = HERE / "runs.json"
WRAPPER_RUNS_DIR = HERE.parent / "wrapper" / "runs"
GATE_PATH = HERE.parent / "wrapper" / "gate.json"

# Tool uses that state a file was written. This is the whole of output
# discovery: if a tool is not here, its writes are not claimed as Outputs.
WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Bash can write too -- heredocs, `sed -i`, `tee`, plain redirection -- and the
# transcript records the command rather than a path. Those writes are COUNTED
# and reported as a coverage gap, never turned into Outputs by parsing shell.
BASH_WRITE_HINT = re.compile(
    r"(^|[\s;&|(])(tee\b|dd\b)|>>?\s*[^\s|&>]|<<\s*['\"]?[A-Za-z_]+|sed\s+-i|"
    r"\bmv\b|\bcp\b|\btouch\b|\binstall\b")

GIT_COMMIT_CMD = re.compile(r"\bgit\b[^\n]*\bcommit\b")
GIT_SHA = re.compile(r"\b([0-9a-f]{7,40})\b")

KIND_BY_SUFFIX = {
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code", ".json": "code",
    ".html": "code", ".css": "code", ".jsonl": "code", ".plist": "code",
    ".md": "document", ".txt": "document", ".yaml": "document", ".yml": "document",
    ".png": "media", ".jpg": "media", ".jpeg": "media", ".svg": "media",
    ".gif": "media", ".pdf": "media", ".mp3": "media", ".mp4": "media",
    ".csv": "analysis",
}
SPEC_HINT = re.compile(r"(^|/)(spec|TASK-\d+|PLAN|PARKED)", re.IGNORECASE)

# Surface, per §2.3. The only evidence a transcript carries is `entrypoint`;
# every record on this machine says claude-desktop. Anything not listed here
# defaults to "code" WITH surface_assumed set, so the assumption is countable
# rather than invisible. Cowork sessions do not write to this tree at all --
# they reach the register through a skill (§7), so they cannot appear here.
SURFACE_BY_ENTRYPOINT = {
    "claude-desktop": "code",
    "cli": "code",
    "claude-code": "code",
}
DEFAULT_SURFACE = "code"

# §2.3's stop_reason vocabulary is about the run; the transcript's stop_reason is
# about the last API turn. Only one mapping is safe. Everything else is null
# with the raw value recorded beside it -- a run whose last turn ended in
# tool_use may have been stopped, may have errored, or may simply not be
# archived to the end yet (the LaunchAgent runs every 10 minutes). Silence is
# not evidence.
STOP_REASON_MAP = {"end_turn": "claude_complete"}


# ------------------------------------------------------------- grouping ----

def group_by_session(records):
    """Fold (record, path) pairs into {session_id: {"records": [...],
    "paths": [...]}}.

    A session can appear under two project folders when its cwd changes
    mid-run; both files are kept and the message-id dedupe below stops the
    overlap being counted twice.
    """
    sessions = {}
    parse_errors = 0
    for record, path in records:
        if record is None:
            parse_errors += 1
            continue
        session_id = record.get("sessionId")
        if not session_id:
            continue
        slot = sessions.setdefault(session_id, {"records": [], "paths": []})
        slot["records"].append(record)
        if path not in slot["paths"]:
            slot["paths"].append(path)
    return sessions, parse_errors


# ------------------------------------------------------------------ refs ----

def run_ref(session_id):
    """A provisional run ref, derived from the session id.

    An earlier session owns the ref scheme and the entity store. §2.3 shows R-0091, a
    sequential ref, and a sequential ref cannot be minted here: the recorder is
    re-run over a growing archive, and a counter assigned in scan order would
    renumber history every time an older transcript arrived. So the ref is
    derived from the session id instead -- stable, collision-free, and
    recomputable from the transcript alone. `claude_session_id` is carried in
    full beside it, which is the natural key an earlier session should join on.
    """
    return "R-" + (session_id or "").replace("-", "")[:8].lower()


def output_ref(run, index):
    return f"O-{run[2:]}-{index:02d}"


# --------------------------------------------------------------- outputs ----

def output_kind(path):
    name = str(path)
    suffix = Path(name).suffix.lower()
    if suffix in (".md", ".txt") and SPEC_HINT.search(name):
        return "spec"
    if "/nanowiki-content/pages/" in name or "/03-wiki/" in name:
        return "wiki"
    return KIND_BY_SUFFIX.get(suffix, "document")


def declared_writes(records):
    """Paths the record says were written, in first-write order.

    The same path written five times in one run is one Output, not five -- the
    write count is kept as evidence of how often, and never as a second Output.
    """
    order = []
    seen = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            key = WRITE_TOOLS.get(block.get("name"))
            if not key:
                continue
            path = ((block.get("input") or {}).get(key) or "").strip()
            if not path:
                continue
            if path not in seen:
                seen[path] = {"path": path, "writes": 0, "tools": set(),
                              "first_seen": record.get("timestamp")}
                order.append(path)
            seen[path]["writes"] += 1
            seen[path]["tools"].add(block.get("name"))
    return [seen[p] for p in order]


def stat_output(path):
    """sha256 and byte count, as at record time.

    This reads a path the record named. It is not discovery -- nothing here
    decides a file is an output because of when it was touched. A file written
    during the run may have been changed or deleted since, so the answer is
    stamped and, where the path is gone, recorded as gone rather than dropped.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return {"exists": False, "sha256": None, "bytes": None,
                "state": "missing_at_record_time"}
    except (IsADirectoryError, PermissionError, OSError) as exc:
        return {"exists": None, "sha256": None, "bytes": None,
                "state": f"unreadable_at_record_time: {type(exc).__name__}"}
    return {"exists": True, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "state": "present_at_record_time"}


def build_outputs(run, records, stat=stat_output):
    outputs = []
    for i, write in enumerate(declared_writes(records), start=1):
        record = {
            "ref": output_ref(run, i),
            "run_ref": run,
            "path": write["path"],
            "kind": output_kind(write["path"]),
            "writes_in_run": write["writes"],
            "written_by": sorted(write["tools"]),
            "first_written_at": write["first_seen"],
        }
        record.update(stat(write["path"]))
        outputs.append(record)
    return outputs


def bash_write_hints(records):
    """How many Bash tool uses looked like they wrote something.

    Reported, never converted into Outputs. A shell command is not a stated
    path, and guessing one from `>` or a heredoc would be exactly the invention
    §7 rules out. The number's job is to say how much of a run's output the
    Outputs list is missing, so a run recorded from a Bash-heavy session is not
    read as a run that wrote nothing.
    """
    hits = 0
    for record in records:
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "Bash":
                continue
            command = (block.get("input") or {}).get("command") or ""
            if BASH_WRITE_HINT.search(command):
                hits += 1
    return hits


# --------------------------------------------------------------- commits ----

def commits_from_records(records):
    """Commit SHAs, read out of the record.

    A `git commit` run through Bash prints its short sha, and the transcript
    keeps that stdout. So the commits are in the record already and no git
    command has to be run to find them -- which matters here, because this
    project forbids git operations outright and not every project directory is
    a repository.
    """
    shas, pending = [], {}
    for record in records:
        if record.get("type") == "assistant":
            for block in (record.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "Bash":
                    continue
                command = (block.get("input") or {}).get("command") or ""
                if GIT_COMMIT_CMD.search(command):
                    pending[block.get("id")] = True
            continue
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or not pending:
            continue
        stdout = result.get("stdout") or ""
        # `[main ad22a2a] message` is git commit's own first line.
        for line in stdout.splitlines()[:3]:
            match = re.match(r"\[[^\]\s]+(?:\s+\(root-commit\))?\s+([0-9a-f]{7,40})\]", line)
            if match and match.group(1) not in shas:
                shas.append(match.group(1))
        pending.clear()
    return shas


# ------------------------------------------------------------------- run ----

def _timestamps(records):
    return sorted(t for t in (r.get("timestamp") for r in records) if t)


def _last_assistant(records):
    last = None
    for record in records:
        if record.get("type") == "assistant" and record.get("timestamp"):
            if last is None or record["timestamp"] >= last["timestamp"]:
                last = record
    return last


def stop_reason_for(records):
    """(stop_reason, api_stop_reason, basis). Null is a real answer here."""
    last = _last_assistant(records)
    api = ((last or {}).get("message") or {}).get("stop_reason")
    if api in STOP_REASON_MAP:
        return STOP_REASON_MAP[api], api, (
            "last assistant turn ended with stop_reason 'end_turn'")
    if api is None:
        return None, None, "no assistant turn in the transcript carries a stop_reason"
    return None, api, (
        f"last assistant turn ended with stop_reason '{api}', which does not map "
        f"onto §2.3's vocabulary; it is not evidence the run was stopped, errored "
        f"or timed out, and the archive may simply not hold the tail yet")


def usage_and_cost(records, prices, claimed=None, session_id=None):
    """The four §2.3 token classes and their price, deduped on message.id.

    Claude Code writes one line per content block and repeats the same usage on
    each, so the dedupe is the load-bearing line -- it is aggregate.py's, and
    the reason this walks the records again rather than reusing collect() is
    only that collect() buckets by (date, project, model) and throws the session
    away.

    `claimed` extends that dedupe ACROSS sessions and is the fix for resumed
    runs. When a session is resumed, Claude Code copies the whole prior
    transcript into a new file and rewrites `sessionId` on every line -- but
    the message ids survive. Priced naively, the shared prefix is charged
    twice: once to the original and again to the fork. `claimed` maps
    message.id -> the session that first priced it, and build() fills it in
    start-time order, so the ORIGIN keeps the cost and the fork keeps only what
    it actually added. Pass None to price a session in isolation, which is what
    the old two-argument call did and what most tests want.
    """
    totals = {"input": 0, "output": 0, "cache_write_5m": 0,
              "cache_write_1h": 0, "cache_read": 0}
    cost = {k: 0.0 for k in aggregate.TOKEN_CLASSES}
    seen, messages, duplicates = set(), 0, 0
    shared_origins = Counter()
    by_model_output = Counter()
    models, unpriced = [], set()
    stray_1h = 0
    ignored = set(prices.get("ignored_models", []))

    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage, model, msg_id = message.get("usage"), message.get("model"), message.get("id")
        if not model or not usage or model in ignored:
            continue
        if msg_id:
            if msg_id in seen:
                duplicates += 1
                continue
            seen.add(msg_id)
            owner = None if claimed is None else claimed.get(msg_id)
            if owner is not None and owner != session_id:
                shared_origins[owner] += 1
                continue
            if claimed is not None and session_id is not None:
                claimed[msg_id] = session_id
        counts = aggregate.usage_counts(usage)
        for key in totals:
            totals[key] += counts[key]
        messages += 1
        if model not in models:
            models.append(model)
        by_model_output[model] += counts["output"]

        price = aggregate.price_for(model, prices)
        if price is None:
            unpriced.add(model)
            continue
        for key, value in aggregate.message_cost(counts, price).items():
            cost[key] += value
        if price.get("cache_write_1h") is None:
            stray_1h += counts["cache_write_1h"]

    usage_block = {
        "input_tokens": totals["input"],
        "output_tokens": totals["output"],
        "cache_creation_input_tokens": totals["cache_write_5m"] + totals["cache_write_1h"],
        "cache_read_input_tokens": totals["cache_read"],
    }
    dominant = by_model_output.most_common(1)
    return {
        "usage": usage_block,
        "usage_detail": {"cache_creation_5m_tokens": totals["cache_write_5m"],
                         "cache_creation_1h_tokens": totals["cache_write_1h"]},
        "cost_usd": round(sum(cost.values()), 6),
        "cost_breakdown": {k: round(v, 6) for k, v in cost.items()},
        "model": dominant[0][0] if dominant else (models[0] if models else None),
        "models": sorted(models),
        "assistant_messages": messages,
        "duplicate_lines_skipped": duplicates,
        "forked_from": shared_origins.most_common(1)[0][0] if shared_origins else None,
        "fork_shared_messages": sum(shared_origins.values()),
        "unpriced_models": sorted(unpriced),
        "cache_write_1h_tokens_priced_at_5m": stray_1h,
    }


def project_for(records, paths):
    """Primary project, plus every cwd the session touched."""
    names = Counter()
    for record in records:
        cwd = record.get("cwd")
        if cwd:
            names[Path(cwd).name] += 1
    if names:
        return names.most_common(1)[0][0], sorted(names)
    fallback = aggregate.project_name({}, paths[0]) if paths else None
    return fallback, [fallback] if fallback else []


def surface_for(records):
    entrypoints = sorted({r.get("entrypoint") for r in records if r.get("entrypoint")})
    for entrypoint in entrypoints:
        if entrypoint in SURFACE_BY_ENTRYPOINT:
            return SURFACE_BY_ENTRYPOINT[entrypoint], False, entrypoints
    return DEFAULT_SURFACE, True, entrypoints


DRIFT_TOLERANCE_USD = 0.01


def link_drift(link, cost_usd, messages):
    """How far a linked run has moved since its figure was written down.

    A run in run-tasks.json `map` is a prior sample, and the register treats it
    as final. It is not: a session can be reopened days later -- on purpose, or
    by clicking an old window -- and start appending under its own id, or be
    resumed into a new id as a fork. 93264808 went $0.79 -> $1.60 -> $2.76 that
    way. A sample that moves after a gate decision was made against it should
    say so out loud rather than quietly changing the priors, so this reports
    rather than corrects.
    """
    recorded = link.get("recorded_usd")
    if recorded is None or cost_usd is None:
        return None, None, ("no recorded_usd on the link, so there is no baseline to "
                            "drift from; add one when the run is linked")
    delta = round(cost_usd - recorded, 6)
    if abs(delta) < DRIFT_TOLERANCE_USD:
        return delta, False, "unchanged since it was linked"
    msgs = link.get("recorded_messages")
    grew = "" if msgs is None else f", {messages - msgs:+d} assistant message(s)"
    return delta, True, (
        f"THIS RUN HAS MOVED SINCE IT WAS LINKED: recorded ${recorded:.2f}, now "
        f"${cost_usd:.2f} ({delta:+.2f}{grew}). A linked run is a prior sample, so a "
        f"moved sample means the priors were derived from a number that no longer holds. "
        f"Re-read it, update recorded_usd in ledger/run-tasks.json with a dated note, and "
        f"re-derive with --priors --write-priors")


def build_run(session_id, slot, prices, rails, links=None, stat=stat_output,
              claimed=None):
    records, paths = slot["records"], slot["paths"]
    ref = run_ref(session_id)
    stamps = _timestamps(records)
    started = stamps[0] if stamps else None
    ended = stamps[-1] if stamps else None
    project, projects = project_for(records, paths)
    rail, rail_assumed = aggregate.rail_for(project, (started or "")[:10], rails)
    surface, surface_assumed, entrypoints = surface_for(records)
    stop_reason, api_stop_reason, stop_basis = stop_reason_for(records)
    priced = usage_and_cost(records, prices, claimed=claimed, session_id=session_id)
    outputs = build_outputs(ref, records, stat=stat)
    link = (links or {}).get(session_id) or {}

    run = {
        "ref": ref,
        "ref_scheme": "session-derived, provisional -- see run_ref() and NOTES",
        "task_ref": link.get("task_ref"),
        "task_class": link.get("task_class"),
        # PARKED.md item 12, decided in an earlier session (PLAN.md row 8). One file
        # served two jobs -- cost ATTRIBUTION and prior ESTIMATION -- and an
        # aborted run wants different answers from each: it is plainly the
        # task's cost, and it is nonsense as a sample of what that class of
        # work costs. They are two fields now. `prior_sample: false` on a map
        # entry attributes the cost and keeps the run out of derive_priors,
        # with the reason on the record rather than implied by a null class.
        # Default true: a linked run is a sample unless someone says why not.
        "prior_sample": link.get("prior_sample", True) is not False,
        "prior_sample_basis": link.get("prior_sample_basis"),
        "task_link_basis": link.get("basis") or (
            "no record links this session to a task; the wrapper has never "
            "executed a run and run-tasks.json has no entry for it"),
        "surface": surface,
        "surface_assumed": surface_assumed,
        "rail": rail,
        "rail_assumed": rail_assumed,
        "claude_session_id": session_id,
        "project": project,
        "projects_touched": projects,
        "model": priced["model"],
        "models": priced["models"],
        "started_at": started,
        "ended_at": ended,
        "status": None,
        "status_basis": (
            "not derivable from a transcript. success/partial/failed is a "
            "judgement about whether the work landed, and nothing in the record "
            "states it; the wrapper would know on execute, and nothing has executed"),
        "stop_reason": stop_reason,
        "api_stop_reason": api_stop_reason,
        "stop_reason_basis": stop_basis,
        "exit_code": None,
        "exit_code_basis": (
            "only the launcher sees an exit code, and it has never executed a run"),
        "transcript_path": str(paths[0]) if paths else None,
        "transcript_paths": [str(p) for p in paths],
        "usage": priced["usage"],
        "usage_detail": priced["usage_detail"],
        "cost_usd": priced["cost_usd"],
        "cost_basis": ("client-side estimate at list rates, priced by aggregate.py "
                       "from the transcript. A shadow price, not a bill") + (
            "" if not priced["fork_shared_messages"] else
            f". THIS RUN IS A RESUME-FORK of {priced['forked_from']}: "
            f"{priced['fork_shared_messages']} assistant message(s) it carries were "
            f"already priced to that session and are NOT priced again here, so this "
            f"figure is what the resumed turns ADDED, not what the file would cost "
            f"read on its own"),
        "forked_from": priced["forked_from"],
        "fork_shared_messages": priced["fork_shared_messages"],
        "cost_breakdown": priced["cost_breakdown"],
        "outputs": [o["ref"] for o in outputs],
        "commits": commits_from_records(records),
        "entrypoints": entrypoints,
        "assistant_messages": priced["assistant_messages"],
        "duplicate_lines_skipped": priced["duplicate_lines_skipped"],
        "unpriced_models": priced["unpriced_models"],
        "cache_write_1h_tokens_priced_at_5m": priced["cache_write_1h_tokens_priced_at_5m"],
        "bash_writes_not_claimed_as_outputs": bash_write_hints(records),
    }
    drift, drifted, drift_basis = link_drift(link, priced["cost_usd"],
                                             priced["assistant_messages"])
    run["link_drift_usd"] = drift
    run["link_drifted"] = drifted
    run["link_drift_basis"] = drift_basis
    return run, outputs


# ------------------------------------------------------------------ join ----

def load_run_tasks(path=RUN_TASKS_PATH):
    """session id -> task, from a hand-written record.

    Not inference. Nothing in a Claude Code transcript states which task a
    session was doing, so the link has to come from somewhere that does. Two
    sources, both records: the launcher's own run record, and this file, which
    is filled in by hand the way rails.json rules are -- dated, reasoned, and
    never guessed from prompt text.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise aggregate.PriceTableError(f"{path} is not valid JSON: {exc}") from exc
    return dict(raw.get("map") or {})


def load_wrapper_runs(directory=WRAPPER_RUNS_DIR):
    """The launcher's run records, joined on session id. Empty today, because
    the launcher only writes one on execute and nothing has executed."""
    links = {}
    if not Path(directory).exists():
        return links
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        session_id = raw.get("session_id_returned") or raw.get("session_id")
        if not session_id:
            continue
        links[session_id] = {
            "task_ref": raw.get("task_ref"),
            "task_class": raw.get("task_class"),
            "basis": f"wrapper run record {path.name}",
        }
    return links


def merge_links(run_tasks, wrapper_runs):
    """The launcher wins where both exist: it was there at launch."""
    merged = dict(run_tasks)
    merged.update({k: v for k, v in wrapper_runs.items() if v.get("task_ref")})
    return merged


# ---------------------------------------------------------------- resume ----

def find_run(runs, session_id):
    for run in runs:
        if run["claude_session_id"] == session_id or run["ref"] == session_id:
            return run
    return None


def resume_payload(run):
    """What `--resume` needs, and the plain statement that it is untested.

    §7: session resumption needs the id stored and nothing more. The launcher
    generates a uuid and passes --session-id; this hands the id back. The path
    has never been exercised, because there is no `claude` CLI on this machine
    -- PARKED.md item 8 -- and this says so rather than implying a tested route.
    """
    return {
        "claude_session_id": run["claude_session_id"],
        "run_ref": run["ref"],
        "project": run["project"],
        "transcript_path": run["transcript_path"],
        "resume_args": ["--resume", run["claude_session_id"]],
        "verified": False,
        "note": ("UNVERIFIED. The id is real and comes from the transcript, but no "
                 "--resume has ever been run from here: there is no `claude` CLI on "
                 "this machine (PARKED.md item 8). This is the id to pass, not a "
                 "route that has been tested."),
    }


# ---------------------------------------------------------------- priors ----

def derive_priors(runs, minimum=3):
    """Cost priors per task class, from Run records rather than by hand.

    This is the thing that makes gate.py's second estimate path real. With
    samples this few "p95" is the worst run observed -- gate.py takes max() and
    labels it that way -- so what is written here is the sample list, not a
    fitted quantile.
    """
    by_class = defaultdict(list)
    excluded = []
    for run in runs:
        task_class = run.get("task_class")
        if not task_class or not run.get("cost_usd"):
            continue
        if run.get("prior_sample") is False:
            excluded.append(run)
            continue
        by_class[task_class].append(run)

    priors = {}
    for task_class, members in sorted(by_class.items()):
        members.sort(key=lambda r: r["started_at"] or "")
        samples = [round(r["cost_usd"], 2) for r in members]
        refs = ", ".join(f"{r.get('task_ref') or r['ref']} ${r['cost_usd']:.2f}"
                         for r in members)
        priors[task_class] = {
            "n": len(samples),
            "samples_usd": samples,
            "basis": (f"Derived by ledger/recorder.py from Run records: {refs}. "
                      f"Each figure is a client-side estimate at list rates, "
                      f"priced from the transcript by aggregate.py."),
            "usable": len(samples) >= minimum,
        }
    # Named, not silently absent. A prior derived from a subset with nothing
    # saying which subset is the failure this whole file exists to avoid --
    # the same discipline `_unaccounted` applies in run-tasks.json.
    for run in excluded:
        entry = priors.get(run["task_class"])
        if entry is None:
            continue
        entry.setdefault("excluded_from_samples", []).append({
            "ref": run.get("task_ref") or run["ref"],
            "cost_usd": round(run["cost_usd"], 2),
            "reason": run.get("prior_sample_basis")
                      or "prior_sample: false in ledger/run-tasks.json",
        })
        entry["basis"] += (
            f" Attributed but NOT sampled: "
            + "; ".join(f"{item['ref']} ${item['cost_usd']:.2f} -- {item['reason']}"
                        for item in entry["excluded_from_samples"]))
    return priors


def write_priors(priors, path=GATE_PATH, now=None):
    """Update gate.json's priors in place, leaving everything else alone.

    Derived classes replace what was there. A class the recorder cannot derive
    is KEPT, not deleted, and stamped source "hand-carried" -- deleting a prior
    because no session has been linked to its class yet would quietly throw away
    a real measurement, and the point of this file is not to lose figures.
    """
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = {}
    for name, prior in (config.get("priors") or {}).items():
        if name not in priors:
            kept = dict(prior)
            kept["source"] = "hand-carried"
            merged[name] = kept
    for name, prior in priors.items():
        stamped = {k: v for k, v in prior.items() if k != "usable"}
        stamped["source"] = "derived by ledger/recorder.py from Run records"
        merged[name] = stamped
    config["priors"] = dict(sorted(merged.items()))
    now = now or datetime.now(timezone.utc)
    config["priors_basis"] = (
        f"Cost per run, derived from Run records by ledger/recorder.py and "
        f"rewritten by `python3 ledger/recorder.py --priors --write-priors` "
        f"(last on {now.date().isoformat()}). Superseded the hand-carried figures "
        f"from ledger/preflight-ledger.md when the Recorder landed. The link from "
        f"a session to a task class comes from ledger/run-tasks.json and from the "
        f"launcher's own run records, never from guessing at prompt text. Adjust "
        f"the threshold here; do not hand-edit a derived prior, re-derive it. A "
        f"prior marked source 'hand-carried' has no linked session yet and is left "
        f"alone rather than deleted. A Run is a session, so a sample is the whole "
        f"session's cost -- a session that covered several tasks is excluded in "
        f"run-tasks.json rather than averaged in.")
    Path(path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


# ------------------------------------------------------------------ main ----

def build(source, prices, rails, links, stat=stat_output):
    sessions, parse_errors = group_by_session(aggregate.iter_transcripts(source))
    runs, outputs = [], []
    # One message.id ledger for the whole pass. The sort is load-bearing: the
    # earliest-starting session claims a message, so a resume-fork is charged
    # only for what it added. See usage_and_cost's `claimed`.
    claimed = {}
    for session_id in sorted(sessions,
                             key=lambda s: (_timestamps(sessions[s]["records"]) or [""])[0]):
        run, run_outputs = build_run(session_id, sessions[session_id], prices,
                                     rails, links, stat=stat, claimed=claimed)
        runs.append(run)
        outputs.extend(run_outputs)
    return runs, outputs, parse_errors


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", default=None,
                        help="transcript root (read-only); defaults to the archive")
    parser.add_argument("--out", default=str(OUT_PATH), help="output JSON path")
    parser.add_argument("--session", default=None,
                        help="print one run by claude session id or run ref, "
                             "with what --resume needs")
    parser.add_argument("--priors", action="store_true",
                        help="print cost priors per task class, derived from Run records")
    parser.add_argument("--write-priors", action="store_true",
                        help="with --priors, write them into wrapper/gate.json")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        prices = aggregate.load_prices()
        rails = aggregate.load_rails()
        links = merge_links(load_run_tasks(), load_wrapper_runs())
    except aggregate.PriceTableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.source:
        source, source_label = Path(args.source), "explicit --source"
    else:
        source, source_label = aggregate.default_source()
    if not Path(source).exists():
        print(f"ERROR: no transcript directory at {source}", file=sys.stderr)
        return 2

    runs, outputs, parse_errors = build(source, prices, rails, links)

    if args.session:
        run = find_run(runs, args.session)
        if run is None:
            print(f"ERROR: no run with session id or ref '{args.session}' in "
                  f"{source}", file=sys.stderr)
            return 2
        print(json.dumps({"run": run,
                          "outputs": [o for o in outputs if o["run_ref"] == run["ref"]],
                          "resume": resume_payload(run)}, indent=2))
        return 0

    minimum = 3
    if GATE_PATH.exists():
        try:
            minimum = json.loads(GATE_PATH.read_text(encoding="utf-8")).get(
                "min_samples_for_prior", 3)
        except json.JSONDecodeError:
            pass
    priors = derive_priors(runs, minimum=minimum)

    if args.priors:
        print(json.dumps(priors, indent=2))
        if args.write_priors:
            write_priors(priors)
            print(f"\n-> priors written to {GATE_PATH}", file=sys.stderr)
        return 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "source_kind": source_label,
        "basis": ("Run and Output records per spec-v0.2.md §2.3 and §2.4. Cost is a "
                  "client-side estimate at list rates on the subscription rail -- a "
                  "shadow price, not a bill. Outputs are the paths the transcript "
                  "states were written; nothing here scans the disk by timestamp."),
        "runs": runs,
        "outputs": outputs,
        "priors": priors,
        "parse_errors": parse_errors,
        "coverage": {
            "runs": len(runs),
            "runs_with_task_ref": sum(1 for r in runs if r["task_ref"]),
            "runs_with_outputs": sum(1 for r in runs if r["outputs"]),
            "outputs_missing_at_record_time": sum(1 for o in outputs if o["exists"] is False),
            "bash_writes_not_claimed_as_outputs":
                sum(r["bash_writes_not_claimed_as_outputs"] for r in runs),
            "surface_assumed_runs": sum(1 for r in runs if r["surface_assumed"]),
            "rail_assumed_runs": sum(1 for r in runs if r["rail_assumed"]),
        },
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _print_summary(payload, out_path)
    return 0


def _print_summary(payload, out_path):
    runs, coverage = payload["runs"], payload["coverage"]
    total = sum(r["cost_usd"] for r in runs)
    print()
    print(f"Source: {payload['source_kind']} — {payload['source']}")
    print(f"{len(runs)} run(s), {len(payload['outputs'])} output(s), "
          f"${total:,.2f} total at list rates (client-side estimate, not a bill).")
    print()
    print(f"{'run':<12}{'started':<18}{'project':<22}{'cost $':>9}{'out':>5}  task")
    print("-" * 78)
    for run in runs:
        started = (run["started_at"] or "")[:16].replace("T", " ")
        print(f"{run['ref']:<12}{started:<18}{(run['project'] or '?')[:21]:<22}"
              f"{run['cost_usd']:>9.2f}{len(run['outputs']):>5}  "
              f"{run['task_ref'] or '—'}")
    print()
    drifted = [r for r in runs if r.get("link_drifted")]
    forks = [r for r in runs if r.get("forked_from")]
    if drifted:
        print("DRIFT — linked runs that have moved since their figure was written:")
        for run in drifted:
            print(f"  {run['ref']:<12}{run['link_drift_usd']:+.2f}  "
                  f"{run['task_ref'] or '—'}")
        print("  These are prior samples. Update recorded_usd in ledger/run-tasks.json "
              "and re-derive.")
        print()
    if forks:
        print("Resume-forks (prefix priced to the origin, not counted twice):")
        for run in forks:
            print(f"  {run['ref']:<12}resumes {run['forked_from'][:8]}  "
                  f"{run['fork_shared_messages']} shared message(s)")
        print()
    if payload["priors"]:
        print("Priors derived from these Run records:")
        for name, prior in payload["priors"].items():
            mark = "usable" if prior["usable"] else "still too thin"
            print(f"  {name:<16} n={prior['n']}  {prior['samples_usd']}  ({mark})")
        print()

    missing = coverage["outputs_missing_at_record_time"]
    if missing:
        print(f"NOTE: {missing} recorded output(s) no longer exist on disk. They are "
              f"kept as records with sha256 and bytes null, not dropped.")
    if coverage["runs"] - coverage["runs_with_task_ref"]:
        print(f"NOTE: {coverage['runs'] - coverage['runs_with_task_ref']} of "
              f"{coverage['runs']} runs have no task ref. task_ref is null rather "
              f"than guessed; add the link in ledger/run-tasks.json.")
    if coverage["bash_writes_not_claimed_as_outputs"]:
        print(f"WARNING: {coverage['bash_writes_not_claimed_as_outputs']} Bash tool "
              f"call(s) looked like they wrote files. Bash states a command, not a "
              f"path, so those writes are NOT in the Outputs above and the Output "
              f"list for a Bash-heavy run is incomplete. Counted, never invented.")
    if coverage["surface_assumed_runs"]:
        print(f"NOTE: {coverage['surface_assumed_runs']} run(s) fall through to "
              f"surface '{DEFAULT_SURFACE}' with an unrecognised entrypoint.")
    print(f"NOTE: {coverage['rail_assumed_runs']} of {coverage['runs']} runs take the "
          f"default rail with no matching rule in rails.json.")
    if payload["parse_errors"]:
        print(f"WARNING: {payload['parse_errors']:,} unparseable line(s) skipped.")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    sys.exit(main())
