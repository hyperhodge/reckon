# SPDX-License-Identifier: Apache-2.0
"""Purge with consent -- spec-v0.2.md §8.5.

    Session transcripts and working files age out by disuse and are queued for
    deletion; nothing is deleted without a yes. Governance records and ledger
    rows are exempt and never purge.

**This module does not delete anything, and that is the design, not a gap.**
`PARKED.md` item 14 took the decision and argued it: the archive exists because
seven weeks of transcripts were pruned by something else, and the one occasion
the record was worth actual money had already been deleted. A purge tool that
deleted would be the second thing on this machine that can delete history. The
governance artefact §8.5 asks for is the *queue* and the *consent record* --
what aged out, by which rule, who said yes and when. The deletion itself is one
`rm` a human runs, and it is the only irreversible step, which makes it the only
one worth a human's hand on it.

So: `plan()` builds the queue, `consent_record()` writes the yes into
`decisions/*.jsonl` beside the export consents and the stage transitions, and
`command_for()` prints the exact line to paste. Nothing here opens a file for
writing except the decision log.

Three rules the tests hold this to:

1. **Exemption is a property of the SET.** `projects/`, `tasks/`, `runs/`,
   `risk/`, `decisions/` and `store/refs.json` can never be *in* the candidate
   set -- not "are skipped by the current run". The test asserts it against a
   plan built over a tree that deliberately contains all of them.
2. **Disuse comes from a named field of a record, never from the filesystem.**
   A transcript's date is `ended_at` on its Run record; a working file's is
   `first_written_at` on the Output record naming it. A file no record names is
   `undecidable` and is left alone. `recorder.py` has a test asserting no
   `mtime` call appears in it at all; the same test runs against this module.
3. **Dry run is the default**, as `wrapper/launch.py` does. Here "dry run" is
   the only run: what the flag actually gates is whether a consent is *recorded*.
"""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = HERE / "purge.json"

sys.path.insert(0, str(HERE))
import store as S  # noqa: E402  -- the ref register, the decision log and utc_now


PURGE_CONSENT_KIND = "PurgeConsent"

#: What a candidate can be. `undecidable` is the interesting one: it is not a
#: near-miss, it is the module refusing to guess. An earlier session's RI-10 is the same
#: shape -- a check that cannot decide says so rather than picking an answer a
#: human would then learn to override.
ELIGIBLE = "eligible"
TOO_RECENT = "too_recent"
HELD = "held"
UNDECIDABLE = "undecidable"


class PurgeRefused(S.StoreError):
    """Anything this module will not do."""


def load_config(path=CONFIG_PATH):
    with open(path) as handle:
        return json.load(handle)


def _expand(path):
    return Path(os.path.expanduser(str(path)))


def exempt_paths(config, root=ROOT):
    """§8.5's exempt set, resolved to absolute paths.

    Returned as a set so the test can assert *membership*, which is the
    assertion `PARKED.md` item 14 asked for: an exemption that is only a branch
    inside a loop is one refactor away from not existing.
    """
    return {(_expand(root) / entry).resolve() for entry in config["exempt_paths"]}


def read_only_roots(config):
    """Roots nothing may ever name, let alone delete. `~/.claude/projects` is
    read-only by CLAUDE.md's standing rule and is also the wrong target: Claude
    Code prunes it on its own schedule, which is what took the seven weeks."""
    return {_expand(entry).resolve() for entry in config["read_only_roots"]}


def is_protected(candidate_path, config, root=ROOT):
    """True if the path is exempt, inside an exempt directory, or inside a
    read-only root. One function, so the queue and the assertion agree by
    construction rather than by both being written correctly."""
    try:
        resolved = Path(candidate_path).resolve()
    except OSError:
        resolved = Path(candidate_path)
    for guarded in exempt_paths(config, root) | read_only_roots(config):
        if resolved == guarded or guarded in resolved.parents:
            return True
    return False


def _age_days(last_used_at, now):
    """Whole days between two ISO-8601 UTC strings, compared as text-free dates.

    Deliberately not `datetime.now()` inside: `now` is passed in so a test can
    fix it, the way the rest of the store does.
    """
    from datetime import datetime

    def parse(value):
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)

    return (parse(now) - parse(last_used_at)).days


def _held_session_ids(run_tasks, gate):
    """Sessions standing behind a number the gate uses today.

    Held is not exemption: exemption is a path rule and this is a fact about one
    file. A transcript cited in `run-tasks.json` is the evidence for a cost
    attribution, and one behind a `samples_usd` figure in `gate.json` is the
    evidence for a prior the gate decides on. Deleting either leaves a live
    number with nothing behind it.
    """
    held = {}
    for session_id, entry in (run_tasks.get("map") or {}).items():
        if session_id.startswith("_"):
            continue
        held[session_id] = "cited in ledger/run-tasks.json as the session for " + str(
            entry.get("task_ref") or entry.get("ref") or "a task")
    for session_id in (run_tasks.get("deliberately_unlinked") or {}):
        if session_id.startswith("_"):
            continue
        held.setdefault(
            session_id,
            "recorded in ledger/run-tasks.json as deliberately unlinked -- the "
            "decision not to link it is itself the record")
    # Rule 2's holding pen: a session read before it ended, waiting for its
    # settled figure. It is precisely the record that must not disappear before
    # someone comes back to it.
    for session_id in (run_tasks.get("held_under_rule_2") or {}):
        if session_id.startswith("_"):
            continue
        held.setdefault(
            session_id,
            "held under rule 2 in ledger/run-tasks.json -- read before it ended "
            "and still owed a settled figure")
    for task_class, prior in (gate.get("priors") or {}).items():
        for cited in _refs_in_prior(prior):
            held.setdefault(
                cited,
                f"stands behind the derived {task_class} prior in wrapper/gate.json")
    return held


def _refs_in_prior(prior):
    """The `R-xxxxxxxx` aliases named in a prior's basis line.

    The basis is prose written by `recorder.py --priors`, and the session ids in
    it are the eight hex characters after `R-`. Parsed rather than assumed
    because the alternative is re-deriving the priors here, and a ninth number
    to reconcile is a bug.
    """
    import re

    found = set()
    for text in [prior.get("basis") or ""] + [
            entry.get("ref") or "" for entry in (prior.get("excluded_from_samples") or [])]:
        found.update(re.findall(r"\bR-([0-9a-f]{8})\b", str(text)))
    return found


def transcript_candidates(config, runs, held, now, archive_root=None):
    """Archived transcripts, aged by `ended_at` on the Run record.

    The join is the session id, which is the transcript's filename stem and a
    named field on the Run -- `claude_session_id`. A transcript with no Run
    record is `undecidable`: the register has no date for it that came from a
    record, and the filesystem's date is not an answer to the question.
    """
    root = _expand(archive_root or config["candidate_roots"]["transcripts"])
    by_session = {}
    for run in runs:
        session_id = run.get("claude_session_id")
        if session_id:
            by_session[session_id] = run
    candidates = []
    if not root.exists():
        return candidates
    for path in sorted(root.rglob("*.jsonl")):
        session_id = path.stem
        run = by_session.get(session_id)
        candidate = {
            "path": str(path),
            "kind": "transcript",
            "session_id": session_id,
            "bytes": path.stat().st_size,
        }
        if run is None or not run.get("ended_at"):
            candidate.update({
                "status": UNDECIDABLE,
                "reason": ("no Run record names this session, so there is no "
                           "named field to age it by. The filesystem knows when "
                           "the file was touched and that is not the same "
                           "question. Left alone."),
                "last_used_at": None,
                "last_used_field": None,
                "last_used_source": None,
            })
            candidates.append(candidate)
            continue
        candidate.update({
            "last_used_at": run["ended_at"],
            "last_used_field": "ended_at",
            "last_used_source": f"Run {run.get('ref') or session_id}",
            "age_days": _age_days(run["ended_at"], now),
        })
        # gate.json cites sessions by their R-xxxxxxxx alias, which is the
        # first eight characters; run-tasks.json cites the full id. Both are
        # the same session and both hold it.
        _apply_rule(candidate, config,
                    held.get(session_id) or held.get(session_id[:8]))
        candidates.append(candidate)
    return candidates


def working_file_candidates(config, outputs, now, root=ROOT):
    """Working files, aged by `first_written_at` on the Output record.

    Only files an Output record actually names are considered. Walking the
    project tree and ageing whatever is found would be the mtime heuristic
    wearing a different hat: the register's claim is that it knows what was
    produced, and a file it has no record of is not something it may delete.
    """
    candidates = []
    for output in outputs:
        path = output.get("path")
        if not path:
            continue
        candidate = {
            "path": path,
            "kind": "working_file",
            "output_ref": output.get("ref"),
            "output_kind": output.get("kind"),
            "bytes": output.get("bytes"),
        }
        if is_protected(path, config, root):
            candidate.update({
                "status": HELD,
                "reason": ("exempt path (§8.5): governance records and ledger "
                           "rows never purge"),
                "last_used_at": output.get("first_written_at"),
                "last_used_field": "first_written_at",
                "last_used_source": f"Output {output.get('ref')}",
            })
            candidates.append(candidate)
            continue
        written_at = output.get("first_written_at")
        if not written_at:
            candidate.update({
                "status": UNDECIDABLE,
                "reason": ("the Output record carries no first_written_at, so "
                           "there is no named field to age it by. Left alone."),
                "last_used_at": None,
                "last_used_field": None,
                "last_used_source": f"Output {output.get('ref')}",
            })
            candidates.append(candidate)
            continue
        candidate.update({
            "last_used_at": written_at,
            "last_used_field": "first_written_at",
            "last_used_source": f"Output {output.get('ref')}",
            "age_days": _age_days(written_at, now),
        })
        if output.get("exists") is False:
            candidate.update({
                "status": HELD,
                "reason": ("the Output record says the file was already gone at "
                           "record time, so there is nothing here to queue"),
            })
        else:
            _apply_rule(candidate, config, None)
        candidates.append(candidate)
    return candidates


def _apply_rule(candidate, config, held_reason):
    """The one place a status is decided, so the queue and the summary cannot
    disagree about what a candidate is."""
    if held_reason:
        candidate["status"] = HELD
        candidate["reason"] = held_reason
        return
    if candidate["age_days"] < config["age_days"]:
        candidate["status"] = TOO_RECENT
        candidate["reason"] = (
            f"{candidate['age_days']} days old against a {config['age_days']}-day "
            f"rule, read from {candidate['last_used_field']} on "
            f"{candidate['last_used_source']}")
        return
    candidate["status"] = ELIGIBLE
    candidate["reason"] = (
        f"{candidate['age_days']} days since {candidate['last_used_field']} on "
        f"{candidate['last_used_source']}, against a {config['age_days']}-day rule")


def plan(config=None, runs=None, outputs=None, run_tasks=None, gate=None,
         now=None, root=ROOT, archive_root=None):
    """The queue. Nothing is opened for writing and nothing is deleted.

    Returns a manifest in the shape `export_manifest` uses -- counts, the rule
    that produced them, and the candidates themselves -- because the consent
    record folds it in whole and has to be readable years later without
    re-running the tool that produced it.
    """
    config = config or load_config()
    now = now or S.utc_now()
    runs = runs if runs is not None else _load_runs(root)
    outputs = outputs if outputs is not None else _load_outputs(root)
    run_tasks = run_tasks if run_tasks is not None else _load_json(
        root / "ledger" / "run-tasks.json", {})
    gate = gate if gate is not None else _load_json(
        root / "wrapper" / "gate.json", {})

    held = _held_session_ids(run_tasks, gate)
    candidates = transcript_candidates(config, runs, held, now, archive_root)
    candidates += working_file_candidates(config, outputs, now, root)

    # The belt to the braces. Every candidate is filtered against the exempt set
    # regardless of how it got here, so a future source added to this function
    # cannot route around §8.5 by forgetting to check.
    for candidate in candidates:
        if candidate["status"] == ELIGIBLE and is_protected(candidate["path"], config, root):
            candidate["status"] = HELD
            candidate["reason"] = "exempt path (§8.5) -- caught by the final filter"

    counts = {}
    for candidate in candidates:
        counts[candidate["status"]] = counts.get(candidate["status"], 0) + 1
    eligible = [c for c in candidates if c["status"] == ELIGIBLE]
    return {
        "generated_at": now,
        "rule": {
            "age_days": config["age_days"],
            "basis": config["age_days_basis"],
            "ageing_read_from": ("a named field of a record -- ended_at on the Run "
                                 "for a transcript, first_written_at on the Output "
                                 "for a working file. Never a filesystem mtime."),
        },
        "exempt_paths": sorted(str(p) for p in exempt_paths(config, root)),
        "read_only_roots": sorted(str(p) for p in read_only_roots(config)),
        "counts": counts,
        "eligible_bytes": sum(c.get("bytes") or 0 for c in eligible),
        "candidates": candidates,
        "deletes_nothing": True,
        "deletes_nothing_basis": (
            "This tool never deletes. It queues and it records the consent; the "
            "rm is a human's. PARKED.md item 14."),
    }


def _load_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _load_runs(root=ROOT):
    """Run records from the canonical append-only store, current versions only.

    `ledger/runs.json` is the feed and `runs/*.jsonl` is canonical, so the
    canonical side is what a deletion decision reads.
    """
    records = S.read_decisions(root / "runs") if (root / "runs").exists() else []
    # current_runs is keyed by ref -- the values are the records.
    return list(S.current_runs(records).values())


def _load_outputs(root=ROOT):
    return (_load_json(root / "ledger" / "runs.json", {}) or {}).get("outputs") or []


def command_for(manifest, shell="sh"):
    """The exact command a human runs, and nothing runs it here.

    One `rm` per line rather than one line with every path on it, so a human
    reading it can stop halfway and so a paste that goes wrong goes wrong for
    one file. Paths are `shlex.quote`d because this project's own directories
    have spaces in them.
    """
    eligible = [c for c in manifest["candidates"] if c["status"] == ELIGIBLE]
    if not eligible:
        return None
    lines = ["# Purge queue, %s. %d file(s), %s."
             % (manifest["generated_at"], len(eligible),
                _human_bytes(manifest["eligible_bytes"])),
             "# Nothing above ran this. Read it, then run it yourself.",
             "# Consent is recorded separately: store/purge.py --consent-by ... "
             "--consent-reason ..."]
    lines += ["rm -i -- %s" % shlex.quote(c["path"]) for c in eligible]
    return "\n".join(lines)


def _human_bytes(total):
    total = total or 0
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f}{unit}" if unit == "B" else f"{total:.1f}{unit}"
        total /= 1024.0


def consent_record(manifest, by=None, reason=None, at=None):
    """The DecisionRecord a purge writes.

    Same shape and same demand as `store.consent_record` for an export, and for
    an earlier session's reason: a consent that cannot be written down is not one. No ref
    is allocated -- §2 gives refs to Projects, Tasks and Runs and none to a log
    entry.

    The manifest is folded in whole, minus the candidate list, which is summarised
    to its eligible paths: the log has to stay readable, and the paths are the
    part a person will want years later when asking what was agreed to.
    """
    if not (by and str(by).strip()):
        raise S.ConsentNotRecorded(
            "a purge needs a name: pass --consent-by. §8.5 says nothing is "
            "deleted without a yes, and a yes with nobody's name on it is not "
            "one.")
    if not (reason and str(reason).strip()):
        raise S.ConsentNotRecorded(
            "a purge needs a reason: pass --consent-reason. What was deleted is "
            "recoverable from a backup; why it was agreed to is not.")
    eligible = [c for c in manifest["candidates"] if c["status"] == ELIGIBLE]
    return {
        "kind": PURGE_CONSENT_KIND,
        "at": at or S.utc_now(),
        "by": str(by).strip(),
        "reason": str(reason).strip(),
        "rule": manifest["rule"],
        "counts": manifest["counts"],
        "eligible_bytes": manifest["eligible_bytes"],
        "queued_paths": [c["path"] for c in eligible],
        "queued_basis": [
            {"path": c["path"], "last_used_at": c["last_used_at"],
             "last_used_field": c["last_used_field"],
             "last_used_source": c["last_used_source"]}
            for c in eligible],
        "undecidable": [c["path"] for c in manifest["candidates"]
                        if c["status"] == UNDECIDABLE],
        "executed_by_this_tool": False,
        "executed_by_this_tool_basis": (
            "This record is a consent to delete, not evidence that anything was "
            "deleted. store/purge.py has no delete path. Whether the rm was run "
            "is a fact about the machine, and the honest answer here is that "
            "this file does not know."),
    }


#: `path`, `queued_paths` and `reason` are disclosive by an earlier session's rule --
#: paths name projects and free text is free text. They are named fields so the
#: redactor can reach them; a disclosive value that lives inside a formatted
#: string cannot be redacted, which is what an earlier session's audit kept finding.
DISCLOSIVE_PURGE_FIELDS = frozenset({"path", "queued_paths", "queued_basis", "reason"})


def format_plan(manifest):
    lines = ["purge queue -- §8.5, and this tool deletes nothing",
             f"generated {manifest['generated_at']}",
             f"rule     ages out after {manifest['rule']['age_days']} days of disuse,",
             "         read from a named field of a record, never an mtime"]
    order = [ELIGIBLE, TOO_RECENT, HELD, UNDECIDABLE]
    lines.append("")
    for status in order:
        count = manifest["counts"].get(status, 0)
        lines.append(f"{status:<12} {count}")
    lines.append("")
    lines.append(f"eligible {_human_bytes(manifest['eligible_bytes'])} across "
                 f"{manifest['counts'].get(ELIGIBLE, 0)} file(s)")
    for candidate in manifest["candidates"]:
        if candidate["status"] in (ELIGIBLE, UNDECIDABLE):
            lines.append(f"  [{candidate['status']}] {candidate['path']}")
            lines.append(f"      {candidate['reason']}")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Purge with consent (§8.5). Queues and records; never deletes.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--archive-root", default=None,
                        help="override the transcript archive root. Testing.")
    parser.add_argument("--command", action="store_true",
                        help="print the rm command for a human to run. Prints "
                             "it; never runs it.")
    parser.add_argument("--consent-by", default=None,
                        help="who is saying yes. Required to RECORD a consent.")
    parser.add_argument("--consent-reason", default=None,
                        help="why. Recorded in decisions/, append-only.")
    parser.add_argument("--consent-log", action="store_true",
                        help="every purge consent that has been recorded")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def purge_consents(directory=None):
    records = S.read_decisions(directory or S.DECISIONS_DIR)
    return [r for r in records if r.get("kind") == PURGE_CONSENT_KIND]


def main(argv=None, out=None):
    args = parse_args(argv)
    out = out or sys.stdout
    root = Path(args.root).resolve()

    def say(line=""):
        print(line, file=out)

    if args.consent_log:
        records = purge_consents(root / "decisions")
        if not records:
            say("no purge has been consented to.")
            return 0
        for record in records:
            say(f"{record['at']}  {record['by']}  "
                f"{len(record.get('queued_paths') or [])} file(s)")
            say(f"    {record['reason']}")
        return 0

    config = load_config(args.config)
    manifest = plan(config=config, root=root, archive_root=args.archive_root)

    if args.json:
        say(json.dumps(manifest, indent=2))
        return 0

    say(format_plan(manifest))

    if args.command:
        command = command_for(manifest)
        say()
        say(command or "# nothing is eligible. No command to run.")

    if args.consent_by or args.consent_reason:
        try:
            record = consent_record(manifest, args.consent_by, args.consent_reason)
        except S.ConsentNotRecorded as refusal:
            say()
            say(f"REFUSED: {refusal}")
            return 2
        S.append_decisions([record], root / "decisions")
        say()
        say(f"consent recorded: {record['by']}, {len(record['queued_paths'])} "
            f"file(s) queued.")
        say("Nothing was deleted. This tool has no delete path -- run the "
            "command yourself.")
    else:
        say()
        say("dry run (the default, and the only run). Nothing was written and "
            "nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
