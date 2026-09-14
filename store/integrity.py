#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 store/store.py --check            (this runs first, always)
          python3 store/store.py --integrity
          python3 store/store.py --integrity-update
          python3 store/store.py --integrity-accept --by NAME --reason TEXT

The append-only tripwire. `runs/`, `decisions/` and `risk/` are append-only by
rule; every writing path in this project obeys that, and until 10 September 2026
NOTHING VERIFIED IT. The rule that a correction is a new record and never an
edit was the register's central claim and it was unaudited.

THREE THINGS SHAPE THIS FILE.

1. APPEND-ONLY IS A TESTABLE PROPERTY: THE EARLIER BYTES NEVER CHANGE. So this
   understands no records at all. It holds a byte length and a hash of exactly
   those bytes, re-hashes the same prefix, and says `edited` when an earlier
   byte moved and `truncated` when the length fell. A stale baseline verifies a
   shorter prefix, never a wrong one -- staleness costs coverage, not truth.

2. A MALFORMED FINAL LINE IS ITS OWN VERDICT, by name. It is the signature of an
   interrupted write, and it is the one failure here that is silent: a reader
   that skips it returns a smaller number and reports no error, which is absent
   data presented as fact. It is never counted as a parse error and never
   skipped.

3. IT REPORTS AND IT NEVER REPAIRS. No delete path, no rollback, no putting a
   number quietly back. A lost record is a finding, and the correction is a new
   record saying so. Advancing the baseline over a violation is not a repair
   either: it needs a name and a reason and is written to decisions/*.jsonl,
   append-only, so the violation outlives the acceptance of it.

The watch list and the baseline are data, in store/integrity.json, in the manner
of prices.json and purge.json. No third-party dependencies, no network, no git --
git sees only committed state, only from 7 September 2026, and only when somebody
reads a diff, which is why it is not the answer here.
"""

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = HERE / "integrity.json"

ACCEPTANCE_KIND = "IntegrityAcceptance"

# The verdicts. Each names one thing that happened to a file, and the severity
# says whether `--check` goes red. `appended` and `unchanged` are the two clean
# ones and are never reported as findings -- a check that talks when nothing is
# wrong is a check people stop reading.
UNCHANGED = "unchanged"
APPENDED = "appended"
EDITED = "edited"
TRUNCATED = "truncated"
RECORDS_LOST = "records_lost"
INTERRUPTED_WRITE = "interrupted_write"
MALFORMED_LINE = "malformed_line"
EXTENDED = "extended"
MISSING = "missing"
UNRECORDED = "unrecorded"

PROBLEM_VERDICTS = {EDITED, TRUNCATED, RECORDS_LOST, INTERRUPTED_WRITE,
                    MALFORMED_LINE, EXTENDED, MISSING}


class IntegrityError(Exception):
    """The tripwire could not be read. Never raised for a violation -- a
    violation is a finding, not an exception."""


def config_for(root):
    """The baseline belongs to the tree it describes, so it is found from the
    root rather than from this module's own location. A tree with no baseline
    file has nothing to verify -- that is an empty watch, not a clean one, and
    the caller is the one that has to say which."""
    return Path(root) / "store" / "integrity.json"


def load_config(path=None):
    # Resolved at call time, never bound as a default, so a test can point the
    # module at a throwaway copy and never at the real baseline.
    return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))


def save_config(config, path=None):
    """The config is data and is edited in place -- it is NOT one of the
    append-only files it watches, and confusing the two would make the tripwire
    unable to record that it had moved."""
    path = Path(path or CONFIG_PATH)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Fingerprinting. Bytes first, records second, and the two are kept apart:
# a record count is a fact about a reader's opinion of the bytes, and the
# bytes are the thing the rule is actually about.
# --------------------------------------------------------------------------

def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def prefix_hash(path, length):
    """The hash of exactly the first `length` bytes, or None if the file is
    shorter than that -- which is itself the answer to a different question."""
    data = Path(path).read_bytes()
    if len(data) < length:
        return None
    return sha256_of(data[:length])


def line_report(data):
    """Records, and the state of the last line. A complete append always ends
    in a newline (append_runs writes one per record), so a final line without
    one is an interrupted write even when what it holds happens to parse."""
    if not data.strip():
        return {"records": 0, "final_line": "empty", "malformed_lines": []}
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    terminated = text.endswith("\n")
    if terminated:
        lines = lines[:-1]
    records, malformed = 0, []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except (ValueError, UnicodeDecodeError):
            malformed.append(index)
            continue
        records += 1
    last_index = len(lines)
    final_malformed = last_index in malformed
    if final_malformed or not terminated:
        final = "interrupted"
    else:
        final = "complete"
    return {"records": records, "final_line": final,
            # A malformed line that is NOT the last one is a different animal:
            # nothing interrupts the middle of a file, so it is corruption or an
            # edit, and it is reported under its own verdict.
            "malformed_lines": [index for index in malformed if index != last_index]}


def fingerprint(path, kind="ledger"):
    path = Path(path)
    data = path.read_bytes()
    record = {"bytes": len(data), "sha256": sha256_of(data)}
    if kind == "ledger":
        record.update(line_report(data))
    return record


# --------------------------------------------------------------------------
# The watch list.
# --------------------------------------------------------------------------

def watched_files(root=ROOT, config=None):
    """Every file the watch list covers, as paths relative to the root, mapped
    to the kind of rule they are held to. Sorted, because the report is read by
    a person and an unstable order reads as churn."""
    config = config or load_config()
    root = Path(root)
    excluded = {entry["path"] for entry in config.get("not_watched") or []}
    found = {}
    for watch in config.get("watched") or []:
        directory = root / watch["root"]
        if not directory.exists():
            continue
        for path in sorted(directory.glob(watch["pattern"])):
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            found[relative] = watch["kind"]
    return dict(sorted(found.items()))


def missing_roots(root=ROOT, config=None):
    config = config or load_config()
    root = Path(root)
    return [watch["root"] for watch in config.get("watched") or []
            if not (root / watch["root"]).exists()]


# --------------------------------------------------------------------------
# The check itself.
# --------------------------------------------------------------------------

def _finding(path, verdict, detail):
    return {"path": path, "verdict": verdict, "detail": detail,
            "severity": "problem" if verdict in PROBLEM_VERDICTS else "advisory"}


def _compare(relative, kind, current, recorded, path):
    """One file against its baseline. Returns every finding it earns, because a
    file can be two things at once -- a mid-line truncation is both a loss and
    an interrupted write, and reporting only the first would hide the shape of
    what happened."""
    findings = []
    if kind == "ledger":
        if current["final_line"] == "interrupted":
            findings.append(_finding(
                relative, INTERRUPTED_WRITE,
                "the last line is incomplete -- the signature of a write that "
                "was interrupted. It is not a parse error and it must not be "
                "skipped: a reader that skips it reports a smaller number and "
                "no error at all."))
        for index in current.get("malformed_lines") or []:
            findings.append(_finding(
                relative, MALFORMED_LINE,
                f"line {index} does not parse and it is not the last line, so "
                f"nothing was interrupted here -- an earlier record was damaged"))

    if recorded is None:
        findings.append(_finding(
            relative, UNRECORDED,
            "not in the baseline, so nothing verifies it yet. Expected for a "
            "file written since the last baseline; run --integrity-update"))
        return findings

    recorded_bytes = recorded.get("bytes", 0)
    if current["bytes"] < recorded_bytes:
        findings.append(_finding(
            relative, TRUNCATED,
            f"{recorded_bytes} bytes were recorded and {current['bytes']} are "
            f"there now -- {recorded_bytes - current['bytes']} bytes have gone"))
    else:
        seen = prefix_hash(path, recorded_bytes)
        if seen != recorded.get("sha256"):
            findings.append(_finding(
                relative, EDITED,
                f"the first {recorded_bytes} bytes no longer hash to what was "
                f"recorded -- an earlier byte moved, and a correction here is a "
                f"new record and never an edit (§8.5)"))
        elif kind == "snapshot" and current["bytes"] > recorded_bytes:
            findings.append(_finding(
                relative, EXTENDED,
                f"the snapshot grew by {current['bytes'] - recorded_bytes} "
                f"bytes. A risk snapshot is append-only as a whole file and a "
                f"correction is a new dated file (§6)"))

    if kind == "ledger" and current.get("records", 0) < recorded.get("records", 0):
        findings.append(_finding(
            relative, RECORDS_LOST,
            f"{recorded.get('records')} records were recorded and "
            f"{current.get('records')} parse now"))
    return findings


def verify(root=ROOT, config=None):
    """What the tripwire says today. It never writes and it never raises for a
    violation: a violation is a finding."""
    config = config or load_config()
    root = Path(root)
    baseline = config.get("baseline") or {}
    files = watched_files(root, config)

    findings, current_state = [], {}
    for relative, kind in files.items():
        path = root / relative
        current = fingerprint(path, kind)
        current_state[relative] = current
        findings.extend(_compare(relative, kind, current, baseline.get(relative), path))

    for relative in sorted(baseline):
        if relative not in files:
            recorded = baseline[relative]
            findings.append(_finding(
                relative, MISSING,
                f"recorded at {recorded.get('bytes', 0)} bytes and it is not "
                f"there now. Nothing here deletes a record"))

    for name in missing_roots(root, config):
        # A watched folder that has never held anything is not a loss -- an
        # empty store has no runs/ yet, and calling that a missing record would
        # make the check cry wolf on the one tree where nothing can be wrong.
        knew_of = [rel for rel in baseline if rel.startswith(f"{name}/")]
        if knew_of:
            findings.append(_finding(
                name, MISSING,
                f"a watched folder is not on disk and the baseline holds "
                f"{len(knew_of)} file(s) from it"))
        else:
            findings.append(_finding(
                name, UNRECORDED,
                "a watched folder is not on disk. Nothing has ever been "
                "recorded from it, so this is an empty watch, not a loss"))

    covered = sum(baseline[rel].get("bytes", 0) for rel in baseline if rel in files)
    on_disk = sum(state["bytes"] for state in current_state.values())
    return {
        "findings": findings,
        "problems": [f for f in findings if f["severity"] == "problem"],
        "advisories": [f for f in findings if f["severity"] == "advisory"],
        "current": current_state,
        "coverage": {"files": len(files), "recorded": len(baseline),
                     "bytes_on_disk": on_disk, "bytes_covered": covered},
        "clean": not any(f["severity"] == "problem" for f in findings),
    }


# --------------------------------------------------------------------------
# Advancing the baseline. Never over a violation without a recorded decision.
# --------------------------------------------------------------------------

def update_baseline(root=ROOT, config=None, now=None, accepted=()):
    """Advance the baseline to what is on disk, for every file that is clean.

    A file with a problem is REFUSED, and the refusal is the whole point: if the
    baseline advanced over an edit, the tripwire would report it once and then
    agree with it forever. `accepted` names the paths a person has consented to
    advance anyway; recording that consent is store.py's job, not this one."""
    config = config or load_config()
    result = verify(root, config)
    baseline = dict(config.get("baseline") or {})
    stamp = now or ""
    advanced, refused = [], []

    blocked = {}
    for finding in result["problems"]:
        blocked.setdefault(finding["path"], []).append(finding["verdict"])

    for relative, current in result["current"].items():
        verdicts = blocked.get(relative)
        if verdicts and relative not in accepted:
            refused.append({"path": relative, "verdicts": sorted(set(verdicts))})
            continue
        previous = baseline.get(relative) or {}
        entry = dict(current)
        entry.pop("final_line", None)
        entry.pop("malformed_lines", None)
        entry["first_seen"] = previous.get("first_seen") or stamp
        entry["last_verified"] = stamp
        if verdicts:
            # An accepted violation is not a clean file and the baseline says so
            # for as long as the entry lives. The decision record in decisions/
            # is the evidence; this is the pointer to it.
            entry["accepted_over"] = sorted(set(verdicts))
        # `advanced` means the FILE moved, not that the check ran again. A
        # timestamp bumped on every clean file would report eight changes on a
        # day one record was appended, which is a report nobody can read.
        moved = {k: v for k, v in entry.items() if k != "last_verified"}
        if {k: v for k, v in previous.items() if k != "last_verified"} != moved:
            advanced.append(relative)
        baseline[relative] = entry

    config["baseline"] = dict(sorted(baseline.items()))
    return config, {"advanced": advanced, "refused": refused, "verify": result}
