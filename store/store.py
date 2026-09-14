#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 store/store.py --check
          python3 store/store.py --list projects|tasks|runs
          python3 store/store.py --show P-0001
          python3 store/store.py --ingest-runs [--dry-run]
          python3 store/store.py --export [--out FILE]

The entity store: Projects, Tasks and Runs on disk, per spec-v0.2.md §8.3.
Markdown with YAML front matter for Projects and Tasks, append-only JSONL for
Runs. An earlier session -- see PLAN.md.

THREE RULES THAT SHAPE THIS FILE.

1. The front matter parser is wrapper/launch.py's, imported rather than
   rewritten. That is not tidiness: the store's files have to stay readable by
   the launcher, and the only way to guarantee that is to use its parser. This
   module adds typing and inline lists on top of what it returns, and never
   writes a shape it cannot read back through it. A test asserts the round trip.

2. Refs are allocated once and written down, never recomputed. ledger/recorder.py
   cannot mint a sequential ref because it re-derives every run from a growing
   archive, and a counter assigned in scan order renumbers history the moment an
   older transcript arrives. A store does not have that problem: allocation is a
   durable write in store/refs.json, bound to a natural key and idempotent on it.
   The Recorder's derived R-xxxxxxxx refs become aliases and are never rewritten,
   because ledger/run-tasks.json and wrapper/gate.json's prior basis cite them.

3. Corrections are new records, never edits (§8.5). runs/YYYY-MM.jsonl is
   append-only and the current view is the last record per ref. This is not
   ceremony -- a run read before its tail reaches the archive is a floor, and
   an earlier session's own run was read at $4.75 and settled four minutes later at $5.81.
   Superseding preserves both and says which was which.

No third-party dependencies, no network, no git. The transcript tree is never
read here at all -- ledger/recorder.py owns that and this module ingests its
output.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "wrapper"))

import launch  # noqa: E402  -- front matter parsing lives there; see rule 1

sys.path.insert(0, str(HERE))
import integrity  # noqa: E402  -- the append-only tripwire; see integrity.py

PROJECTS_DIR = ROOT / "projects"
TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs"
REFS_PATH = HERE / "refs.json"
LEDGER_RUNS_PATH = ROOT / "ledger" / "runs.json"
DECISIONS_DIR = ROOT / "decisions"

PROJECT_RE = re.compile(r"^P-(\d{4})$")
TASK_RE = re.compile(r"^P-(\d{4})-T(\d{2})$")
RUN_RE = re.compile(r"^R-(\d{4})$")
RUN_ALIAS_RE = re.compile(r"^R-[0-9a-f]{8}$")

INBOX_PROJECT = "P-0000"   # the unsorted inbox (§3.2)

PROJECT_STAGES = ["captured", "screened", "scoped", "prototype",
                  "validated", "production", "retired"]
TASK_STATES = ["drafted", "approved", "running", "built", "shipped"]
TASK_OFF_RAMP = "abandoned"
PROJECT_OFF_RAMPS = {"parked": ["parked_until", "parked_reason"],
                     "killed": ["killed_reason"]}

# §3.1. What a stage owes, and the exit test it owes it against. Cumulative:
# a project at `validated` owes everything the earlier stages owed as well.
# ENFORCED FROM SESSION 6. `stage_debt()` still only reports -- it is the
# question, not the answer -- and `transition_project()` now refuses an
# advancing transition that owes anything, names every debt and its exit test,
# and accepts a recorded override. What did NOT change is §3.1's principle:
# fields become mandatory as a project *advances*, never at creation. Capture
# is still two fields and eight seconds, a record already sitting in a stage it
# owes fields for is still readable, and reading is never gated.
STAGE_REQUIREMENTS = {
    "captured": {
        "fields": ["name", "one_liner"],
        "exit_test": "Screened within 14 days or auto-flagged stale",
    },
    "screened": {
        "fields": ["context", "type", "value_basis"],
        "exit_test": "Problem statement and provisional value, or Killed",
    },
    "scoped": {
        "fields": ["is_ai_system", "my_role", "act_tier", "directory"],
        "exit_test": "Data sources named, first risk assessment, success metric",
    },
    "prototype": {
        "fields": ["output_location", "value_estimate"],
        "exit_test": "Working artefact linked, model recorded, effort logged",
    },
    "validated": {
        "fields": ["third_party_output"],
        "exit_test": "Run on real instances, validation method, risk reassessed",
    },
    "production": {
        "fields": ["next_review"],
        "exit_test": "Reviews on cadence, or retirement decision",
    },
    "retired": {
        "fields": [],
        "exit_test": "Terminal. Record and learnings retained",
    },
}

# §2.1's governance block -- "the fields a client asks to see". Stored since
# an earlier session and not reasoned about until an earlier session.
#
# `is_ai_system` is three-valued: yes | no | contested. `_coerce` already
# refuses to turn "yes"/"no" into a bool for exactly this reason, and the third
# value is not decoration -- "contested" is the honest answer for a system whose
# classification is argued about, and a boolean has nowhere to put it.
#
# Which stage owes which field is NOT restated here. It is read out of
# STAGE_REQUIREMENTS, so there is one list and not two.
CLASSIFICATION_VOCABULARY = {
    "is_ai_system": ["yes", "no", "contested"],
    "my_role": ["provider", "deployer", "both", "n/a"],
    "act_tier": ["prohibited", "high", "transparency", "minimal", "out_of_scope"],
    "third_party_output": ["none", "internal", "client", "published"],
}

CLASSIFICATION_FIELDS = tuple(CLASSIFICATION_VOCABULARY)


def classification_owed_from(field):
    """The stage a classification field first becomes mandatory at, read out of
    the one table that already knows. None means no stage owes it."""
    for stage in PROJECT_STAGES:
        if field in STAGE_REQUIREMENTS[stage]["fields"]:
            return stage
    return None


def classify(fields):
    """§2.1's governance block for one project, with an opinion about each field.

    Returns {field: {value, state, owed_from, allowed}} where `state` is one of
    `set`, `missing` or `invalid`, plus roll-ups. Reports; never refuses --
    a record already on disk stays readable whatever it says.
    """
    stage = fields.get("stage") or "captured"
    known = stage in PROJECT_STAGES
    result, missing, invalid = {}, [], []
    for field, allowed in CLASSIFICATION_VOCABULARY.items():
        value = fields.get(field)
        owed_from = classification_owed_from(field)
        owed_now = bool(known and owed_from and
                        PROJECT_STAGES.index(owed_from) <= PROJECT_STAGES.index(stage))
        if value in (None, "", "null"):
            state = "missing"
            if owed_now:
                missing.append(field)
        elif str(value) not in allowed:
            state = "invalid"
            invalid.append(field)
        else:
            state = "set"
        result[field] = {"value": value, "state": state, "owed_from": owed_from,
                         "owed_now": owed_now, "allowed": list(allowed)}
    return {
        "ref": fields.get("ref"),
        "stage": stage,
        "fields": result,
        "missing_and_owed": missing,
        "invalid": invalid,
        "complete": not missing and not invalid,
        "unclassified": all(result[f]["state"] == "missing"
                            for f in CLASSIFICATION_FIELDS),
    }


# §8.5: "paths can be disclosive even when contents are not". Presentation mode
# is an earlier session's job, and it is only possible if every disclosive value sits in
# a named field a redactor can enumerate -- never in free-text notes, never
# inside a ref.
#
# SESSION 6 ADDED THE SECOND GROUP, and the reason is worth keeping. Building the
# redactor over an earlier session's list left "Separate nanowiki's code from its content"
# in a task `brief` with the project's `name` redacted two lines above it. A
# redactor that hides the label and ships the description is worse than none,
# because it looks like it worked. The audit in `governance.py` found it on the
# first run against the real store, which is the argument for having the audit
# assert against the serialised payload rather than against its own list.
#
# The rule the second group follows: **free text a human wrote about the work
# is disclosive, vocabulary is not.** `context: business` and `act_tier: minimal`
# stay in the clear -- they are the governance a presentation exists to show.
#
# NOT here, deliberately: `claude_session_id` and `claude_project_id`. A uuid
# names nothing about anyone, and redacting the ids while `runs/*.jsonl` is the
# evidence base would cost the presented export its audit trail for no gain.
DISCLOSIVE_FIELDS = frozenset({
    # paths and identity -- §8.5's own sentence
    "directory", "output_location", "github_repo", "spec_path",
    "transcript_path", "path", "name",
    # free text about the work, added an earlier session
    "one_liner", "brief", "value_estimate", "killed_reason", "parked_reason",
    # `outcome` lands here IN THE SAME CHANGE that adds it to §2.2, and the
    # timing is the whole point. An earlier session's audit found three fields that had
    # shipped one session ahead of their redaction -- a risk-dimension note, an
    # Output path, a calibration_points[].project -- and each was caught by the
    # machine refusing to build rather than by anyone reasoning about it. An
    # outcome is free text a human wrote about the work, which is exactly the
    # rule the second group follows, and it is the most disclosive field on the
    # record: "what shipped, what did not, and the one thing to raise" names
    # clients, paths and people by construction.
    "outcome",
    # `parked_ref` points a Task at the section of PARKED.md that holds its long
    # form, and it is a PATH -- which is §8.5's own sentence about disclosive
    # values. Added 7 September in the mop-up that introduced the field, and
    # added because `governance.py --audit` REFUSED TO BUILD and named all eight
    # values, not because anyone reasoned about it beforehand. That is the
    # fourth instance of this shape (an earlier session found three) and the fourth time
    # the audit found it by asserting against the serialised payload rather than
    # against the redactor's own account of what it touched.
    "parked_ref",
    # `decision_owed` lands here IN THE SAME CHANGE that adds the field, for
    # the reason the `outcome` comment above gives: an earlier session's audit found
    # three fields that had shipped one session ahead of their redaction, and
    # `parked_ref` was a fourth on 7 September. It is free text a human wrote
    # about the work -- the second group's rule exactly -- and a sentence
    # naming what the owner has to decide names the thing he has to decide about.
    "decision_owed",
    # `invocation_path` points at the next session's opening prompt, and it is
    # a PATH -- §8.5's own sentence, the same reason `parked_ref` is here. The
    # prompt's TEXT is not front matter (it has newlines) and so never reaches
    # the export; where it reaches a SCREEN it is redacted by register_view,
    # which has its own test, because the store's enumerative redactor cannot
    # see a value the export does not carry.
    "invocation_path",
    # `queued_note` is free text a session wrote about why a handoff was taken
    # out of the queue, and it is here for the SIXTH instance of the same shape.
    # It was found on 7 September, an earlier session, and the way it was found is the
    # point: nothing was reasoned about. The moment `invocation_path` was set on
    # a record again, the path became a disclosive value, and the audit
    # immediately reported that same path surviving inside this note -- a field
    # nobody had thought about because it holds prose, not a location. That is
    # the rule the second group of this set follows, and the audit has now
    # caught it six times where reasoning has caught it none.
    "queued_note",
})

# §2.1 has `cost_to_date_gbp` and it has been null on P-0001 since an earlier session.
# PLAN.md row 8 asked for a stated rule or a deliberate null. THE RULE, decided
# in an earlier session: it stays null, and it stays null by rule rather than by neglect.
#
# The Runs sum to a real USD figure. Every one of them is a client-side estimate
# at LIST rates on the SUBSCRIPTION rail -- a shadow price for work a £20/month
# subscription already paid for. Writing that into a field named "cost to date"
# in pounds would need two things this machine does not have and one it must not
# invent: an FX rate (there is no rate source here, which is also why §10 query
# 14 refuses to divide its two halves), a rule for apportioning a fixed monthly
# subscription across the runs inside the month, and the willingness to let a
# shadow price be read as money that moved. The first is missing, the second is
# arbitrary, and the third is the thing this register exists to prevent.
#
# What is emitted instead, and is not a compromise: `cost_usd` on every Run,
# rolled up Run -> Task -> Project by queries.py, labelled as an estimate at
# list rates every time it is shown. The pound figure that IS real -- the £20
# subscription -- is emitted by query 14 as itself, beside the USD figure and
# deliberately not divided into it.
COST_TO_DATE_GBP_RULE = (
    "Null by rule. Run costs are client-side estimates at list rates on the "
    "subscription rail -- a shadow price, not money that moved -- and there is "
    "no FX rate source on this machine and no non-arbitrary way to apportion a "
    "fixed monthly subscription across the runs inside it. The USD roll-up is "
    "emitted instead, labelled; the £20 subscription is emitted by §10 query 14 "
    "as itself. A converted figure would read as a bill. An earlier session, PLAN.md row 8.")

LIST_FIELDS = frozenset({"depends_on", "runs", "outputs", "commits",
                         "aliases", "data_sources"})


class StoreError(Exception):
    """Anything this module refuses to do."""


class RefCollision(StoreError):
    """A ref was asked for that is already bound to a different natural key."""


class StageGateRefused(StoreError):
    """A stage gate said no. Carries the debts so the caller can print them:
    §3.1's gate has to name what it wants, or it is the flat thirty-field form
    §3.1 rejects wearing a different hat."""

    def __init__(self, message, debts=None, project_ref=None, to_stage=None):
        super().__init__(message)
        self.debts = debts or []
        self.project_ref = project_ref
        self.to_stage = to_stage


class OverrideNotRecorded(StoreError):
    """An override with no name or no reason. §3.1's gates are governance, and
    governance that cannot be overridden gets worked around instead -- but an
    unrecorded workaround is not evidence, so an override that cannot be
    written down is refused before the gate is."""


class AppendOnlyViolation(StoreError):
    """A write would have changed or dropped a line that is already on disk."""


class InterruptedWrite(StoreError):
    """A line in an append-only .jsonl does not parse.

    Named, rather than left as a bare JSONDecodeError, because the shape of this
    failure matters more than the fact of it: a half-written last line is the
    signature of a write that was interrupted, and it is the one loss here that
    a reader can swallow -- skip the line and you report a smaller number and no
    error, which is absent data presented as fact. store/integrity.py gives it a
    verdict; this gives it a name so `--check` can catch it and say the verdict
    instead of showing a traceback."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Front matter: launch.py's parser, plus typing and inline lists.
# --------------------------------------------------------------------------

def _coerce(key, value):
    """Type one front matter value. Inline lists only -- see render_record."""
    if key in LIST_FIELDS or (value.startswith("[") and value.endswith("]")):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(",") if item.strip()]
    if value in ("null", "~", ""):
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    # "yes" / "no" stay strings: §2.1's is_ai_system is yes | no | contested,
    # a three-valued field, and coercing two thirds of it to bool loses the third.
    return value


def parse_record(text):
    """Parse a store record. Returns (fields dict, body str).

    Delegates to launch.py so the store can never write front matter the
    launcher cannot read, then coerces inline `[a, b]` lists and nulls. The
    launcher sees those as opaque strings, which is exactly what it should do
    with a field it has no opinion about.
    """
    front, body = launch.parse_front_matter(text)
    return {key: _coerce(key, value) for key, value in front.items()}, body


def render_record(fields, body=""):
    """Render fields + body back to a store record.

    Inline lists only, never block lists: launch.py's parser rejects an indented
    line, so a block list would make the file unreadable by the launcher. That
    constraint is deliberate and this is where it is enforced.
    """
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join(str(item) for item in value) + "]"
        elif value is None:
            rendered = "null"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
            if "\n" in rendered:
                raise StoreError(
                    f"Field {key!r} holds a newline; front matter here is flat "
                    f"scalars and inline lists only. Put prose in the body.")
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    text = "\n".join(lines) + "\n"
    if body:
        text += "\n" + body.lstrip("\n")
        if not text.endswith("\n"):
            text += "\n"
    return text


def read_record(path):
    return parse_record(Path(path).read_text(encoding="utf-8"))


def write_record(path, fields, body=""):
    text = render_record(fields, body)
    parse_record(text)  # never write what we cannot read back
    Path(path).write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------
# Refs. Allocated once, written down, never recomputed.
# --------------------------------------------------------------------------

class RefRegister:
    """The allocation ledger. Idempotent on the natural key, loud on collision.

    §8.3's layout does not name an allocation register. It is an addition rather
    than an absorption, and it is what makes a sequential ref honest here: the
    Recorder cannot mint one because it recomputes from a growing archive, and
    this can because it writes the allocation down once.
    """

    def __init__(self, data=None, path=REFS_PATH):
        self.path = Path(path)
        self.data = data if data is not None else {
            "_note": ("Ref allocation, spec-v0.2.md §2. Allocated once and written "
                      "here; never recomputed, never reused, never renumbered. "
                      "`bindings` maps a ref to the natural key it was allocated "
                      "for -- allocation is idempotent on that key. `aliases` maps "
                      "an older ref to its canonical one; ledger/recorder.py's "
                      "session-derived R-xxxxxxxx refs live there and are cited by "
                      "ledger/run-tasks.json and wrapper/gate.json, so they are "
                      "resolved, never rewritten."),
            "bindings": {},
            "aliases": {},
        }

    @classmethod
    def load(cls, path=REFS_PATH):
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        return cls(json.loads(path.read_text(encoding="utf-8")), path=path)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")

    # -- allocation ------------------------------------------------------

    def _bind(self, ref, natural_key, kind):
        existing = self.data["bindings"].get(ref)
        if existing is not None:
            if existing["natural_key"] != natural_key:
                raise RefCollision(
                    f"{ref} is already bound to {existing['natural_key']!r} "
                    f"({existing['kind']}); refusing to rebind it to "
                    f"{natural_key!r}. Refs are never reused.")
            return ref
        if ref in self.data["aliases"]:
            raise RefCollision(
                f"{ref} is already an alias of {self.data['aliases'][ref]}; "
                f"refusing to allocate it.")
        self.data["bindings"][ref] = {"natural_key": natural_key, "kind": kind,
                                      "allocated_at": utc_now()}
        return ref

    def _find(self, kind, natural_key):
        for ref, binding in self.data["bindings"].items():
            if binding["kind"] == kind and binding["natural_key"] == natural_key:
                return ref
        return None

    def _next(self, kind, pattern, template, group=1):
        highest = 0
        for ref, binding in self.data["bindings"].items():
            if binding["kind"] != kind:
                continue
            match = pattern.match(ref)
            if match:
                highest = max(highest, int(match.group(group)))
        return template % (highest + 1)

    def allocate_project(self, natural_key):
        found = self._find("project", natural_key)
        if found:
            return found
        return self._bind(self._next("project", PROJECT_RE, "P-%04d"),
                          natural_key, "project")

    def allocate_task(self, project_ref, natural_key):
        found = self._find("task", natural_key)
        if found:
            return found
        highest = 0
        for ref, binding in self.data["bindings"].items():
            if binding["kind"] != "task":
                continue
            match = TASK_RE.match(ref)
            if match and f"P-{match.group(1)}" == project_ref:
                highest = max(highest, int(match.group(2)))
        return self._bind(f"{project_ref}-T{highest + 1:02d}", natural_key, "task")

    def allocate_run(self, claude_session_id):
        """Runs are allocated on claude_session_id, the only identifier the
        transcripts themselves carry (§2.3). It stays the natural key; the
        sequential ref is the human-facing one."""
        found = self._find("run", claude_session_id)
        if found:
            return found
        return self._bind(self._next("run", RUN_RE, "R-%04d"),
                          claude_session_id, "run")

    def adopt(self, ref, natural_key, kind):
        """Record a ref that was minted by hand before this register existed.

        P-0001-T01, T03, T04 and T05 are all in that state, and P-0001-T02 was
        never allocated at all -- the task files were numbered globally while the
        refs were numbered per project. Adoption takes what exists rather than
        renumbering it, so the sequence is allowed to have holes.
        """
        return self._bind(ref, natural_key, kind)

    # -- aliases ---------------------------------------------------------

    def add_alias(self, alias, canonical):
        if alias == canonical:
            raise RefCollision(f"{alias} cannot alias itself.")
        if alias in self.data["bindings"]:
            raise RefCollision(
                f"{alias} is an allocated ref, not an alias; refusing to shadow it.")
        existing = self.data["aliases"].get(alias)
        if existing is not None and existing != canonical:
            raise RefCollision(
                f"{alias} already aliases {existing}; refusing to repoint it "
                f"at {canonical}.")
        if canonical not in self.data["bindings"]:
            raise StoreError(f"Cannot alias to {canonical}: it is not allocated.")
        self.data["aliases"][alias] = canonical
        return canonical

    def resolve(self, ref):
        """Canonical ref for a ref or an alias. Unknown refs come back unchanged
        rather than raising, so a citation in an old file still reads."""
        if ref in self.data["bindings"]:
            return ref
        return self.data["aliases"].get(ref, ref)

    def aliases_of(self, canonical):
        return sorted(alias for alias, target in self.data["aliases"].items()
                      if target == canonical)

    def natural_key(self, ref):
        binding = self.data["bindings"].get(self.resolve(ref))
        return binding["natural_key"] if binding else None


# --------------------------------------------------------------------------
# Stage gates -- advisory (§3.1). They report; they never refuse.
# --------------------------------------------------------------------------

def stage_debt(fields, stage=None):
    """What a project owes at a stage. Cumulative over the earlier stages.

    Returns a list of {field, owed_from, exit_test}. Empty means nothing owed.
    Never raises on a debt -- that is the whole point of `advisory`.
    """
    stage = stage or fields.get("stage") or "captured"
    if stage not in PROJECT_STAGES:
        raise StoreError(f"Unknown stage {stage!r}; expected one of "
                         f"{', '.join(PROJECT_STAGES)}.")
    debts = []
    for name in PROJECT_STAGES[:PROJECT_STAGES.index(stage) + 1]:
        requirement = STAGE_REQUIREMENTS[name]
        for field in requirement["fields"]:
            value = fields.get(field)
            if value in (None, "", [], "null"):
                debts.append({"field": field, "owed_from": name,
                              "exit_test": requirement["exit_test"]})
    return debts


def format_debt(debt):
    """One debt, in the words the advisory has always used. Refusal reuses this
    verbatim: a gate that says no without saying what it wants is the same flat
    form §3.1 rejects, and two phrasings of one debt is two things to keep true."""
    return (f"{debt['field']!r} (from {debt['owed_from']!r}: {debt['exit_test']})")


def transition_direction(from_stage, to_stage):
    """`advance`, `retreat`, `same`, or `retire`.

    Only an advance is gated, and that is the whole of §3.1 in one function.
    A retreat reduces what the record claims, so refusing it would trap a
    project in a stage it has outgrown downwards. `retired` is an exit and its
    own exit test is "Terminal. Record and learnings retained" -- refusing to
    retire an under-documented project would keep it in Production, which is
    the opposite of what the gate is for. Both are reported, neither refused.
    """
    if to_stage == "retired":
        return "retire"
    if from_stage not in PROJECT_STAGES:
        return "advance"           # unknown or unset origin: gate it
    here, there = PROJECT_STAGES.index(from_stage), PROJECT_STAGES.index(to_stage)
    return "advance" if there > here else ("retreat" if there < here else "same")


def check_override(override):
    """An override must carry a name and a reason, or it is not an override.

    Returns the normalised record, or None when nothing was offered."""
    if not override:
        return None
    by = (override.get("by") or "").strip()
    reason = (override.get("reason") or "").strip()
    missing = [name for name, value in (("by", by), ("reason", reason)) if not value]
    if missing:
        raise OverrideNotRecorded(
            f"An override needs {' and '.join(missing)}; §3.1's gates are "
            f"governance and a governance override is only worth anything as "
            f"evidence. Nothing was overridden and nothing was written.")
    return {"by": by, "reason": reason, "at": override.get("at") or utc_now()}


def transition_project(fields, to_stage, now=None, enforce=True, override=None):
    """Move a project to a stage. ENFORCING from an earlier session (PLAN.md row 6).

    Refuses an **advancing** transition into a stage that owes fields, naming
    every debt and its exit test. A retreat, a no-op and retirement report
    their debts and are allowed -- see `transition_direction`.

    An override is a dict `{"by": name, "reason": text}`. It is recorded on the
    transition, never silent, and an override missing either half raises
    `OverrideNotRecorded` before the gate is even consulted.

    `enforce=False` restores an earlier session's advisory behaviour for callers that
    want to preview a move. It is not a way past the gate -- the transition
    record says `enforced: false` and nothing may write that to disk claiming
    otherwise.

    Returns (new_fields, transition_record, debts).
    """
    if to_stage not in PROJECT_STAGES:
        raise StoreError(f"Unknown stage {to_stage!r}.")
    override = check_override(override)
    now = now or utc_now()
    from_stage = fields.get("stage")
    direction = transition_direction(from_stage, to_stage)
    updated = dict(fields)
    updated["stage"] = to_stage
    updated["stage_since"] = now[:10]
    debts = stage_debt(updated, to_stage)
    ref = fields.get("ref")

    gated = enforce and direction == "advance" and bool(debts)
    if gated and not override:
        raise StageGateRefused(
            f"{ref}: cannot advance from {from_stage!r} to {to_stage!r} -- "
            f"{len(debts)} field(s) owed: " + "; ".join(format_debt(d) for d in debts) +
            ". Fill them, or override with a name and a reason (§3.1).",
            debts=debts, project_ref=ref, to_stage=to_stage)

    transition = {                      # §2/§8.5, StageTransition: append-only
        "kind": "StageTransition",
        "project_ref": ref,
        "from_stage": from_stage,
        "to_stage": to_stage,
        "at": now,
        "direction": direction,
        "debts": debts,
        "enforced": bool(enforce),
        "overridden": bool(gated and override),
        "override": override if gated and override else None,
        "basis": _transition_basis(enforce, direction, debts, gated and override),
    }
    return updated, transition, debts


def _transition_basis(enforce, direction, debts, overridden):
    if not enforce:
        return ("advisory: enforcement was switched off by the caller, so this "
                "reports what a stage owes and moves the project anyway")
    if overridden:
        return ("gate refused and was overridden with a name and a reason, "
                "recorded per §3.1 -- an unrecorded workaround is not evidence")
    if not debts:
        return "nothing owed at this stage"
    if direction == "retire":
        return ("retirement is an exit, not an advance: §3.1's exit test is "
                "'Terminal. Record and learnings retained', and refusing it "
                "would hold the project in Production instead")
    if direction == "retreat":
        return ("a retreat reduces what the record claims, so the debts are "
                "reported and the move is allowed")
    return "moving within the same stage; debts reported"


# --------------------------------------------------------------------------
# Task states (§3.2). One hard rule, and it is the P-0000 rule.
# --------------------------------------------------------------------------

# §3.2's own words: "Approval is the cowork-to-code boundary and is recorded
# with a name and a timestamp, because separation of approval from execution is
# the control a client recognises." A control that reports a missing name and
# lets the task through is not that control. From an earlier session it refuses.
TASK_STATE_REQUIREMENTS = {
    "approved": {
        "fields": ["approved_by", "approved_at"],
        "exit_test": ("Approval is the cowork-to-code boundary, recorded with a "
                      "name and a timestamp (§3.2)"),
    },
}


def task_state_debt(fields, state=None):
    """What a task state owes. Not cumulative -- unlike a project stage, a task
    state is a position rather than an accumulation, and `shipped` does not owe
    `approved`'s fields a second time."""
    state = state or fields.get("state") or "drafted"
    requirement = TASK_STATE_REQUIREMENTS.get(state)
    if not requirement:
        return []
    return [{"field": field, "owed_from": state, "exit_test": requirement["exit_test"]}
            for field in requirement["fields"]
            if fields.get(field) in (None, "", [], "null")]


def transition_task(fields, to_state, project_ref=None, now=None,
                    enforce=True, override=None):
    """Move a task to a state.

    Two hard refusals, both §3.2's. The P-0000 rule -- a Task cannot leave
    `drafted` while its project sits in the unsorted inbox -- has been hard
    since an earlier session and stays hard: it is not a stage gate and **it cannot be
    overridden**, because it is a rule about where the work belongs rather than
    about what a record owes. And from an earlier session, entering `approved` without a
    name and a timestamp, which *is* a debt and therefore *is* overridable.

    Everything else -- a skipped state, a move backwards -- is reported and
    allowed, as before.

    Returns (new_fields, notes, transition_record).
    """
    if to_state not in TASK_STATES and to_state != TASK_OFF_RAMP:
        raise StoreError(f"Unknown task state {to_state!r}; expected one of "
                         f"{', '.join(TASK_STATES + [TASK_OFF_RAMP])}.")
    override = check_override(override)
    now = now or utc_now()
    current = fields.get("state") or "drafted"
    project_ref = project_ref or fields.get("project_ref") or ""
    project_ref = project_ref.split()[0] if project_ref else ""

    if current == "drafted" and to_state != "drafted" and project_ref == INBOX_PROJECT:
        raise StoreError(
            f"{fields.get('ref')} cannot leave 'drafted' while its project is "
            f"{INBOX_PROJECT}, the unsorted inbox (§3.2). Sort the project first. "
            f"This one is not overridable: it is a rule about where the work "
            f"belongs, not a field the record owes.")

    notes = []
    if to_state in TASK_STATES and current in TASK_STATES:
        skipped = TASK_STATES[TASK_STATES.index(current) + 1:TASK_STATES.index(to_state)]
        if skipped:
            notes.append(f"skipped {', '.join(skipped)} -- advisory, not refused")
        if TASK_STATES.index(to_state) < TASK_STATES.index(current):
            notes.append(f"moved backwards from {current} -- advisory, not refused")

    updated = dict(fields)
    updated["state"] = to_state
    debts = task_state_debt(updated, to_state)
    entering = to_state != current
    gated = enforce and entering and bool(debts)
    if gated and not override:
        raise StageGateRefused(
            f"{fields.get('ref')}: cannot enter {to_state!r} -- "
            f"{len(debts)} field(s) owed: " +
            "; ".join(format_debt(d) for d in debts) +
            ". Fill them, or override with a name and a reason (§3.2).",
            debts=debts, project_ref=project_ref, to_stage=to_state)
    if debts and not gated:
        notes.extend(f"owes {format_debt(debt)}" for debt in debts)

    transition = {
        "kind": "TaskTransition",
        "task_ref": fields.get("ref"),
        "project_ref": project_ref or None,
        "from_state": current,
        "to_state": to_state,
        "at": now,
        "debts": debts,
        "enforced": bool(enforce),
        "overridden": bool(gated and override),
        "override": override if gated and override else None,
        "notes": notes,
    }
    return updated, notes, transition


# --------------------------------------------------------------------------
# §2.2's outcome field. PARKED.md item 15, decided by the owner on 6 September:
# "a real omission -- add the field". TASK-016.
#
# `brief` is the instruction. `state` is where the work got to. Nothing held
# WHAT HAPPENED -- the "what shipped, what did not, and the one thing to raise"
# narrative every session wrote into PLAN.md and the next session actually
# read. That was the single most-read thing in this repository and the register
# had nowhere to put it, which is the one reason the scaffolding outlived the
# plan it was scaffolding for.
#
# THREE DECISIONS, all load-bearing:
#
# 1. IT IS A FRONT MATTER FIELD, NOT THE BODY. Bodies are not exported --
#    governance.py says so in its own docstring -- and a value that is not in a
#    named field cannot be redacted. An outcome is free text a human wrote about
#    the work, which is an earlier session's own definition of disclosive, so it has to
#    live somewhere the redactor enumerates. That costs it newlines, because
#    render_record refuses them: entries are one line each, joined by
#    OUTCOME_SEPARATOR. A paragraph fits on a line. A chapter does not, and a
#    chapter belongs in a document -- which is the honest half of item 15's
#    second reading, kept rather than argued away.
#
# 2. APPENDED TO, NEVER REPLACED, and that is the point rather than a nicety.
#    An outcome that gets overwritten is a Status column with extra steps. The
#    reason PLAN.md was worth retiring is that its narrative ACCUMULATED: each
#    session's paragraph sat beside the last one instead of on top of it.
#    Enforced the way runs/*.jsonl is enforced -- a prefix assertion raising the
#    same AppendOnlyViolation -- and every write to a task record now goes
#    through write_task_record, so a state transition cannot quietly drop one.
#
# 3. NO STAGE AND NO STATE OWES IT. Answered explicitly rather than left
#    implicit, and the reasoning is in store/NOTES.md: an outcome is written
#    when work happens, not when a state is entered, and a gate that refused a
#    transition for missing prose would be refusing on narrative.
# --------------------------------------------------------------------------

# Chosen because it does not occur in prose and does not occur in a path. The
# entry text is checked against it rather than trusted: a value carrying the
# separator would split into two entries on the way back out, which is a
# correction nobody made.
OUTCOME_SEPARATOR = " || "


def outcome_entries(value):
    """The outcome field split back into the entries that were appended to it.

    The store's own reader. Anything that wants the entries -- the front end,
    a query, a write-up -- asks here rather than splitting on the separator
    itself, so the separator stays one fact in one place.
    """
    if not value:
        return []
    return [entry.strip() for entry in str(value).split(OUTCOME_SEPARATOR)
            if entry.strip()]


def assert_outcome_append_only(before, after):
    """`after`'s outcome must start with `before`'s, verbatim.

    The same shape as assert_append_only() for runs/*.jsonl and for the same
    reason: a correction is a new entry, never an edit. The second check is
    the one that matters -- "shipped" growing into "shipped it" passes a naive
    prefix test and is still a rewrite of the last entry rather than a new one.
    """
    old = ((before or {}).get("outcome") or "").strip()
    new = ((after or {}).get("outcome") or "").strip()
    if old == new:
        return True
    if not new.startswith(old):
        raise AppendOnlyViolation(
            "outcome was replaced, not appended to: an outcome that can be "
            "overwritten is a Status column with extra steps (§2.2, TASK-016). "
            "A correction is a new entry.")
    if old and not new[len(old):].startswith(OUTCOME_SEPARATOR):
        raise AppendOnlyViolation(
            "the last outcome entry was edited rather than a new one appended "
            "(§2.2). A correction is a new entry.")
    return True


def append_outcome(fields, text, at=None):
    """Append one outcome entry. Returns (new_fields, entry).

    The only door an outcome is written through, the way --capture is the only
    door a Project comes through. Datestamped because the accumulation is the
    value: an entry that cannot say when it was written is a paragraph, not a
    record.
    """
    text = " ".join(str(text or "").split())
    if not text:
        raise StoreError("An outcome with no text is not an outcome.")
    if OUTCOME_SEPARATOR.strip() in text:
        raise StoreError(
            f"An outcome entry cannot contain {OUTCOME_SEPARATOR.strip()!r}: it "
            f"separates entries, and a value carrying it would come back out as "
            f"two entries nobody wrote.")
    entry = f"{(at or utc_now())[:10]}: {text}"
    existing = (fields.get("outcome") or "").strip()
    updated = dict(fields)
    updated["outcome"] = f"{existing}{OUTCOME_SEPARATOR}{entry}" if existing else entry
    assert_outcome_append_only(fields, updated)
    return updated, entry


# --------------------------------------------------------------------------
# `decision_owed` -- what is waiting on the owner, in one sentence. An earlier session.
#
# WHY IT EXISTS, and the reason is a failure rather than a design: `state` says
# where a task IS, `brief` says what it is ABOUT, and neither says what a person
# has to DO. On 7 September the ask on P-0001-T19 sat at line 72 of a 100-line
# record -- "does §5.3 need a third rail value or a second axis" -- and the owner
# could not find it. A register you cannot read to know what to do is a filing
# cabinet. This is the field that answers "what is on me".
#
# THE RULES, and they are what keep it from becoming a second Status column:
#
#   - ONE SENTENCE, and it names a DECISION, not work. THE TEST IS SHARPER
#     THAN "is someone waiting on the owner", because on that test every drafted
#     task qualifies and the view degrades into `--list tasks` again: A
#     DECISION HERE IS ONE THAT CANNOT BE ANSWERED BY "YES, DO IT". A choice
#     between options, a judgement call, or a fact only the owner can go and get.
#     A build task waiting for approval is answered by "yes, do it", is
#     already visible as `drafted`, and stays null.
#   - HAND-WRITTEN, never derived. Reading a task body and inferring what its
#     author wanted decided is guessing, and guessing is the invention §7 rules
#     out. Nothing in this module computes this value.
#   - A LIVE field, not a log. `outcome` is the append-only record and it is
#     where the DECISION goes once it is taken, dated, never rewritten. This
#     field is the QUESTION, and a question that has been answered is cleared.
#     Clearing it writes nothing anywhere, so `--owe --clear` says so out loud
#     when the task has no outcome entry to carry the answer.
#   - NULL MEANS NOTHING IS WAITING ON THE OWNER. It does not mean nothing is
#     waiting. That distinction is the whole value of the field and it is the
#     one a later session will be tempted to erode.
# --------------------------------------------------------------------------


def set_decision_owed(fields, text):
    """Set the one sentence naming what the owner has to decide. Returns fields."""
    text = " ".join(str(text or "").split())
    if not text:
        raise StoreError(
            "A decision owed with no text is not a decision owed. To say that "
            "nothing is waiting, clear it.")
    # ONE PROPOSITION, NOT A CHOICE. The owner, 7 September 2026, after P-0001-T26
    # was recorded as REJECT against "rewrite the README around the app, or
    # retire it in favour of the app explaining itself": "This is neither
    # accept or reject because it presents 2 valid options. which one am i
    # accepting or rejecting? need to phrase questions as accept against a
    # single option. If offering another option, it's another decision and it
    # can be referred to in the first ask."
    #
    # HE IS RIGHT AND HIS OWN ANSWER PROVED IT -- "Keep it for now, I want
    # both" is a third option neither branch offered, recorded as a rejection
    # of a question that cannot be rejected. Five of the seven open questions
    # had the same fault when this was added, all written by the same session
    # that built the buttons, which is what an unstated rule costs.
    #
    # A rejection must MEAN something. For a single proposition it means "do
    # not do that", and where the alternative is simply the negation it needs
    # no record of its own. Where the alternative is real work, it is ANOTHER
    # decision on another record, referred to from this one.
    for word in (" or ", " whether "):
        if word in f" {text.lower()} ":
            raise StoreError(
                f"a decision_owed containing {word.strip()!r} is a CHOICE, and "
                f"Agree/Reject cannot answer a choice -- a rejection of "
                f"'A or B' says nothing about which. State ONE proposition to "
                f"accept or reject. If the alternative is real work, make it "
                f"its own decision and refer to it from this one.")
    if len(text) > 300:
        raise StoreError(
            f"{len(text)} characters. `decision_owed` is ONE SENTENCE naming "
            f"what has to be decided -- the long form belongs in the body, "
            f"which is exactly where it was when nobody could find it. Say the "
            f"decision, point at the body for the reasoning.")
    updated = dict(fields)
    updated["decision_owed"] = text
    return updated


def clear_decision_owed(fields):
    """Clear it. Returns (fields, warning or None).

    The warning is not decoration. This field is the question and `outcome` is
    the answer; clearing without an outcome entry deletes the question and
    records nothing, which is how a decision comes to have been taken by
    nobody, on no date, for no reason.
    """
    updated = dict(fields)
    updated["decision_owed"] = None
    warning = None
    if not outcome_entries(fields.get("outcome")):
        warning = ("cleared, and this task has NO outcome entry -- so the "
                   "decision itself is now recorded nowhere. Append it: "
                   "store.py --outcome REF --text \"...\"")
    return updated, warning


def decisions_owed(store):
    """Every task with a decision waiting on a person, ref-sorted.

    A view, not a check: it reports what was written down and computes nothing.
    """
    owed = []
    for ref, task in sorted(store["tasks"].items()):
        sentence = task["fields"].get("decision_owed")
        if sentence:
            owed.append({"ref": ref,
                         "state": task["fields"].get("state"),
                         "brief": task["fields"].get("brief"),
                         "decision_owed": sentence,
                         "outcome": outcome_entries(task["fields"].get("outcome"))})
    return owed


# --------------------------------------------------------------------------
# The handoff chain. An earlier session, 7 September 2026, on the owner's instruction.
#
# THE RULE THAT CREATED IT, in his words: whenever a session says "I'd stop
# here, that is a new session", it must either start the next one or hand over
# the invocation -- and the handoff must be recorded so the sessions form a
# chain going forwards rather than a series of endings.
#
# WHY A FIELD RATHER THAN A PARAGRAPH. A session that ends with advice in its
# closing message ends. The advice is in a transcript nobody re-reads, and the
# next session starts from nothing and pays to rediscover the context -- which
# is most of what a long session's first twenty turns cost here. `invocation`
# is the opening prompt, written by the session that has the context, stored on
# the task that needs it, and READ OFF A SCREEN rather than typed.
#
# THE CHAIN IS TWO FIELDS AND NO NEW RECORD TYPE. `handed_off_by` names the run
# that wrote the invocation and `handed_off_at` dates it. Backwards: a task
# names the session that set it up. Forwards: that session's own task carries
# the next invocation. Refs are permanent and a fourth numbering scheme to
# identify a handoff would be a convention invented rather than followed.
#
# WHAT THIS IS NOT: an approval. An invocation is a prepared prompt, not a
# budget and not a go-ahead. The gate still prices it and the owner still says yes.
# --------------------------------------------------------------------------


HANDOFFS_DIR = ROOT / "handoffs"


def invocation_path_for(ref, root=ROOT):
    return Path(root) / "handoffs" / f"{ref}.md"


def read_invocation(fields, root=ROOT):
    """The invocation text, or None. Reads the file the record points at."""
    rel = fields.get("invocation_path")
    if not rel:
        return None
    path = Path(root) / rel
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def set_invocation(fields, text, root=ROOT, by=None, at=None):
    """Write the next session's opening prompt and point the record at it.

    The text goes in a FILE, not in front matter: front matter here is flat
    scalars and an invocation is prose with newlines. That is not a workaround
    -- `spec_path` already works this way, and it keeps the prompt diffable and
    readable on its own.

    Deliberately NOT capped the way `decision_owed` is. A decision is one
    sentence because a person must find it at a glance; an invocation is a
    brief because a session must act on it without asking. Conflating the two
    is how a brief became a hundred-line record nobody could read.
    """
    text = str(text or "").strip()
    if not text:
        raise StoreError("An invocation with no text is not a handoff.")
    ref = fields.get("ref")
    if not ref:
        raise StoreError("An invocation needs a task ref to belong to.")
    path = invocation_path_for(ref, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    updated = dict(fields)
    updated["invocation_path"] = str(path.relative_to(Path(root)))
    updated["handed_off_by"] = by or None
    updated["handed_off_at"] = (at or utc_now())
    return updated


def next_sessions(store):
    """Every task carrying an invocation, ref-sorted. A view; computes nothing."""
    ready = []
    root = store.get("root") or ROOT
    for ref, task in sorted(store["tasks"].items()):
        invocation = read_invocation(task["fields"], root)
        if not invocation:
            continue
        ready.append({"ref": ref,
                      "state": task["fields"].get("state"),
                      "brief": task["fields"].get("brief"),
                      "invocation": invocation,
                      "handed_off_by": task["fields"].get("handed_off_by"),
                      "handed_off_at": task["fields"].get("handed_off_at"),
                      "decision_owed": task["fields"].get("decision_owed")})
    return ready


def write_task_record(path, fields, body="", previous=None):
    """The only door a task record is written through.

    Re-reads what is on disk rather than trusting the copy in memory, which is
    deliberate: PARKED.md item 17 is two windows open on this project at once
    with no merge protection, and a task record written from a store loaded ten
    minutes ago is exactly the clobber that describes. If the file grew an
    outcome in the meantime, this refuses instead of dropping it.
    """
    path = Path(path)
    if previous is None and path.exists():
        previous, _ = read_record(path)
    assert_outcome_append_only(previous or {}, fields)
    return write_record(path, fields, body)


# --------------------------------------------------------------------------
# The plan, in the register. PLAN.md row ordering, moved in by TASK-016.
#
# `plan_row` is a LIST, not a scalar, and the reason is a fact about the plan
# rather than a preference: the eight rows and the task records are not 1:1.
# P-0001-T10 was written for rows 7 AND 8 and ran twice, once for each; row 8
# then took a third run under P-0001-T13. A scalar would have to pick one and
# be wrong about the other.
#
# What is NOT here: the rows' titles, phases or narrative. This module knows
# how many rows there are, because it has to check that every one of them is
# claimed by something; it does not restate what they say. Two files claiming
# to know what a row IS is the outcome PARKED.md item 15 explicitly rules out,
# and a constant in code would be a third.
# --------------------------------------------------------------------------

PLAN_ROWS = 8


def plan_rows_of(fields):
    """The plan rows a task claims, as ints. Tolerant of a scalar on disk."""
    value = fields.get("plan_row")
    if value in (None, "", "null", []):
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    rows = []
    for item in values:
        try:
            rows.append(int(str(item).strip()))
        except ValueError:
            rows.append(str(item).strip())
    return rows


def plan_view(store):
    """The eight rows, read back out of the register (§2.2, TASK-016).

    The read-back deliverable 3 was told not to trust an assumption about. It
    reports, it does not repair: a row with no task and a task with a row
    outside the plan are both things to see rather than things to fix here.
    """
    rows = {row: [] for row in range(1, PLAN_ROWS + 1)}
    stray = {}
    for ref, task in sorted(store["tasks"].items()):
        for row in plan_rows_of(task["fields"]):
            entry = {"ref": ref, "state": task["fields"].get("state"),
                     "depends_on": list(task["fields"].get("depends_on") or []),
                     "outcome": outcome_entries(task["fields"].get("outcome")),
                     "brief": task["fields"].get("brief")}
            if isinstance(row, int) and 1 <= row <= PLAN_ROWS:
                rows[row].append(entry)
            else:
                stray.setdefault(str(row), []).append(entry)
    return {"rows": rows, "stray": stray,
            "unclaimed": [row for row, tasks in rows.items() if not tasks],
            "without_outcome": [row for row, tasks in rows.items()
                                if tasks and not any(t["outcome"] for t in tasks)]}


# --------------------------------------------------------------------------
# The decisions log (§8.5): StageTransition and DecisionRecord are append-only,
# and corrections are new records. An earlier session built `transition_project()` and
# persisted nothing, on the argument that an empty append-only log is not a
# feature. An earlier session gives it something to hold: the moment a gate refuses and
# is overridden anyway is exactly the record §3.1 means by "an override must be
# recorded". Same file shape and the same refusals as `runs/*.jsonl`, imported
# rather than redefined.
#
# No ref is allocated here. §2 gives refs to Projects, Tasks and Runs and gives
# none to a transition; minting a fourth scheme to number a log nobody cites by
# number would be inventing a convention rather than following one. A record is
# identified by (project_ref or task_ref, at).
# --------------------------------------------------------------------------

def decisions_path(at, directory=DECISIONS_DIR):
    return Path(directory) / f"{at[:7]}.jsonl"


def read_decisions(directory=DECISIONS_DIR):
    directory = Path(directory)
    records = []
    for path in sorted(directory.glob("*.jsonl")):
        records.extend(read_runs_file(path))
    return records


def append_decisions(records, directory=DECISIONS_DIR):
    """Append transition/decision records, grouped by month. Append-only."""
    written = []
    by_file = {}
    for record in records:
        by_file.setdefault(decisions_path(record["at"], directory), []).append(record)
    for path, group in sorted(by_file.items()):
        append_runs(path, group)        # the same "a" -only writer, and the same refusal
        written.append((path, len(group)))
    return written


# --------------------------------------------------------------------------
# Capture. §3.1: two fields and eight seconds, and a stage that owes nothing
# else yet. This is the only door a new Project comes through, and it is here
# rather than in the front end because a ref is allocated, never minted --
# app/ is a read view over generated data and cannot write to disk at all
# (§8.1). The browser queues text; this turns text into a record.
# --------------------------------------------------------------------------

# §2.1's Project fields, at their capture-time values. Every governance field
# is null rather than absent: §3.1 makes fields mandatory as a project
# advances, never at creation, and stage_debt() can only report a debt on a
# field it can see. A missing key and a null key are not the same thing here.
CAPTURE_DEFAULTS = {
    "context": None, "type": None, "stage": "captured", "stage_since": None,
    "parked_until": None, "killed_reason": None, "depends_on": [],
    "is_ai_system": None, "my_role": None, "act_tier": None,
    "third_party_output": None, "directory": None, "git": "local",
    "output_location": None, "claude_project_id": None,
    "preferences_inherited": True, "budget_tokens_default": 50000,
    "value_basis": None, "value_estimate": None,
    "inherent_risk": None, "residual_risk": None, "next_review": None,
    "cost_to_date_gbp": None,
}

CAPTURE_BODY = """# {ref} — {name}

{one_liner}

Captured {at} through `store.py --capture`. Two fields, per §3.1: everything else is null and is
owed by a later stage, not by this one. `store.py --check` will say what `captured` owes; it will
not refuse anything.
"""


def capture_slug(name):
    """The natural key a captured project binds its ref to. Lowercased and
    squeezed, because 'Nano Wiki' and 'nano  wiki' are the same project and
    allocating twice would spend two refs on one thing."""
    return " ".join(str(name).strip().lower().split())


def capture_project(one_liner, register, name=None, root=ROOT, now=None):
    """Capture a project. Allocates a ref through the register, writes
    projects/P-NNNN.md, and returns (ref, path, debts, created).

    Idempotent on the slug: capturing the same name twice returns the same ref
    and does not overwrite the record on disk. Refs are never reused, so
    allocating a second one for the same thing is the expensive mistake.
    """
    one_liner = " ".join(str(one_liner or "").split())
    if not one_liner:
        raise StoreError("A capture needs a one-liner. §3.1 asks for two fields; "
                         "this is the one that cannot be defaulted.")
    name = " ".join(str(name or one_liner).split())
    if len(name) > 60:                    # a one-liner is not a name
        name = name[:57].rstrip() + "..."
    now = now or utc_now()

    ref = register.allocate_project(capture_slug(name))
    path = Path(root) / "projects" / f"{ref}.md"
    if path.exists():
        fields, _ = read_record(path)
        return ref, path, stage_debt(fields), False

    fields = {"ref": ref, "name": name, "one_liner": one_liner}
    fields.update(CAPTURE_DEFAULTS)
    fields["stage_since"] = now[:10]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_record(path, fields, CAPTURE_BODY.format(
        ref=ref, name=name, one_liner=one_liner, at=now))
    return ref, path, stage_debt(fields), True


# §2.2's Task fields at capture. The same order a hand-written record carries,
# so a captured task reads like every other one and nothing downstream has to
# know how it was made. Everything past the brief is null or empty: a task is
# drafted here and earns the rest on its way to approved.
CAPTURE_TASK_DEFAULTS = {
    "claude_task_id": None, "task_class": None, "task_type": None,
    "state": "drafted", "stage_at_start": None, "brief": None,
    "spec_path": None, "spec_sha": None, "approved_by": None, "approved_at": None,
    "budget_tokens": None, "est_p95_usd": None, "plan_row": [], "depends_on": [],
    "runs": [], "invocation_path": None, "decision_owed": None, "outcome": None,
    "handed_off_by": None, "handed_off_at": None,
}

CAPTURE_TASK_BODY = """Captured {at} through `store.py --capture-task`.

{one_liner}
"""


def capture_task_slug(one_liner):
    """The natural key a captured task binds its ref to, in the hyphenated shape
    the hand-allocated task keys in refs.json already have."""
    return re.sub(r"[^a-z0-9]+", "-", str(one_liner).lower()).strip("-")[:80].rstrip("-")


def capture_task(project_ref, one_liner, register, root=ROOT, now=None):
    """Capture a task under a project. Allocates the next task number through
    the register, writes tasks/P-NNNN-TNN.md through write_task_record, and
    returns (ref, path, created). The register is NOT saved here; the caller
    saves it only when created is True.

    Refuses a project that has no record and an empty one-liner. Never
    overwrites a task file: the same wording twice returns the ref it already
    has and writes nothing. This exists because on 10 September a session
    minted a task ref by hand and saved an empty register over the real one
    (P-0001-T46) -- a command that does it is the fix.
    """
    project_ref = str(project_ref or "").strip()
    if not PROJECT_RE.match(project_ref):
        raise StoreError(f"{project_ref!r} is not a project ref; expected P-NNNN.")
    if not (Path(root) / "projects" / f"{project_ref}.md").exists():
        raise StoreError(f"{project_ref} is not in the store: there is no "
                         f"projects/{project_ref}.md. Capture the project first.")
    one_liner = " ".join(str(one_liner or "").split())
    slug = capture_task_slug(one_liner)
    if not slug:
        raise StoreError("A task capture needs a one-liner with words in it.")
    now = now or utc_now()

    ref = register.allocate_task(project_ref, slug)
    if not ref.startswith(f"{project_ref}-T"):
        raise StoreError(f"{ref} is already bound to this wording under another "
                         f"project; refs are never reused. Word it differently.")
    path = Path(root) / "tasks" / f"{ref}.md"
    if path.exists():
        return ref, path, False

    fields = {"ref": ref, "project_ref": project_ref}
    fields.update(CAPTURE_TASK_DEFAULTS)
    fields["brief"] = one_liner
    path.parent.mkdir(parents=True, exist_ok=True)
    write_task_record(path, fields, CAPTURE_TASK_BODY.format(at=now, one_liner=one_liner))
    return ref, path, True


# --------------------------------------------------------------------------
# Runs. runs/YYYY-MM.jsonl is canonical; ledger/runs.json derives from the
# archive and is ingested into it. See NOTES.md for why round that way.
# --------------------------------------------------------------------------

def runs_path(started_at, directory=RUNS_DIR):
    return Path(directory) / f"{started_at[:7]}.jsonl"


def read_runs_file(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as problem:
            raise InterruptedWrite(
                f"{path}: line {number} does not parse ({problem}). "
                f"See --integrity for the verdict.") from problem
    return records


def append_runs(path, records, existing=None):
    """Append records. Refuses to alter or drop a line already on disk (§8.5)."""
    path = Path(path)
    on_disk = read_runs_file(path) if existing is None else list(existing)
    if existing is not None and read_runs_file(path)[:len(on_disk)] != on_disk:
        raise AppendOnlyViolation(f"{path} no longer starts with the lines given.")
    if not records:
        return on_disk
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:   # "a" only, never "w"
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return on_disk + list(records)


def assert_append_only(before, after):
    """after must start with before, verbatim. Used by the checker."""
    if after[:len(before)] != before:
        for index, (old, new) in enumerate(zip(before, after)):
            if old != new:
                raise AppendOnlyViolation(
                    f"line {index + 1} changed: a correction is a new record, "
                    f"not an edit (§8.5).")
        raise AppendOnlyViolation("lines were removed; the log is append-only (§8.5).")
    return True


def current_runs(records):
    """The current view: the last record per ref. Earlier ones are superseded
    and kept, because a run read before its tail is archived is a floor."""
    view = {}
    for record in records:
        view[record["ref"]] = record
    return view


RUN_FACTS = ("cost_usd", "usage", "started_at", "ended_at", "status",
             "stop_reason", "outputs", "commits", "task_ref", "model")


def run_from_ledger(record, ref, aliases, now=None):
    """One store Run from one ledger/runs.json Run.

    Cost is copied, never recomputed. ledger/recorder.py imports its pricing
    from aggregate.py and the two reconcile to $0.000002; a third arithmetic
    here would be a third number to reconcile.
    """
    return {
        "ref": ref,
        "aliases": list(aliases),
        "claude_session_id": record["claude_session_id"],
        "task_ref": record.get("task_ref"),
        "task_class": record.get("task_class"),
        "surface": record.get("surface"),
        "rail": record.get("rail"),
        "model": record.get("model"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "status": record.get("status"),
        "stop_reason": record.get("stop_reason"),
        "exit_code": record.get("exit_code"),
        "usage": record.get("usage"),
        "cost_usd": record.get("cost_usd"),
        # The Recorder already lists outputs by ref on the Run; take them as
        # given rather than re-deriving. Its Output records are the detail.
        "outputs": [output if isinstance(output, str) else output.get("ref")
                    for output in record.get("outputs") or []],
        "commits": record.get("commits", []),
        "transcript_path": record.get("transcript_path"),
        "recorded_at": now or utc_now(),
        "recorded_from": "ledger/runs.json, written by ledger/recorder.py",
        "cost_basis": record.get("cost_basis"),
        "supersedes": None,
    }


def differs(old, new):
    return [fact for fact in RUN_FACTS if old.get(fact) != new.get(fact)]


def ingest_runs(ledger_runs, register, existing, now=None):
    """Turn ledger Runs into store Runs. New ones are appended; changed ones are
    appended as corrections that supersede. Unchanged ones are skipped.

    Returns (records_to_append, report).
    """
    now = now or utc_now()
    view = current_runs(existing)
    by_session = {record["claude_session_id"]: record for record in view.values()}
    appended, report = [], {"new": [], "superseded": [], "unchanged": []}

    for record in ledger_runs:
        session = record["claude_session_id"]
        ref = register.allocate_run(session)
        provisional = record.get("ref")
        if provisional and RUN_ALIAS_RE.match(provisional):
            register.add_alias(provisional, ref)
        built = run_from_ledger(record, ref, register.aliases_of(ref), now=now)
        previous = by_session.get(session)
        if previous is None:
            appended.append(built)
            report["new"].append({"ref": ref, "session": session,
                                  "cost_usd": built["cost_usd"]})
            continue
        changed = differs(previous, built)
        if not changed:
            report["unchanged"].append(ref)
            continue
        built["supersedes"] = {"recorded_at": previous["recorded_at"],
                               "changed": changed,
                               "previous_cost_usd": previous.get("cost_usd")}
        appended.append(built)
        report["superseded"].append({"ref": ref, "changed": changed,
                                     "from_cost_usd": previous.get("cost_usd"),
                                     "to_cost_usd": built["cost_usd"]})
    return appended, report


# --------------------------------------------------------------------------
# Loading and checking the whole store.
# --------------------------------------------------------------------------

def load_store(root=ROOT):
    root = Path(root)
    projects, tasks = {}, {}
    for path in sorted((root / "projects").glob("P-*.md")):
        fields, body = read_record(path)
        projects[fields.get("ref", path.stem)] = {"fields": fields, "body": body,
                                                  "path": str(path)}
    for path in sorted((root / "tasks").glob("P-*.md")):
        fields, body = read_record(path)
        tasks[fields.get("ref", path.stem)] = {"fields": fields, "body": body,
                                               "path": str(path)}
    runs = []
    for path in sorted((root / "runs").glob("*.jsonl")):
        runs.extend(read_runs_file(path))
    decisions = read_decisions(root / "decisions")
    return {"projects": projects, "tasks": tasks, "runs": runs,
            "decisions": decisions, "root": str(root)}


def check(store, register):
    """Every complaint the store can make about itself. Advisory throughout:
    it reports, it does not repair and it does not refuse."""
    problems, advisories = [], []

    for ref, project in store["projects"].items():
        if not PROJECT_RE.match(ref):
            problems.append(f"{ref}: not a valid project ref")
        if register.natural_key(ref) is None:
            problems.append(f"{ref}: not in the ref register (store/refs.json)")
        for debt in stage_debt(project["fields"]):
            advisories.append(
                f"{ref}: stage '{project['fields'].get('stage')}' owes "
                f"'{debt['field']}' (from '{debt['owed_from']}': {debt['exit_test']})")
        # Advisory about debts is not advisory about the vocabulary -- the same
        # rule an unknown stage has always had. A missing classification is a
        # debt (§3.1, reported above); a classification outside §2.1's own list
        # is a record saying something the schema cannot mean.
        governance = classify(project["fields"])
        for field in governance["invalid"]:
            detail = governance["fields"][field]
            problems.append(
                f"{ref}: {field} is {detail['value']!r}, which is not one of "
                f"{' | '.join(detail['allowed'])} (§2.1)")
        stage = project["fields"].get("stage")
        for off_ramp, required in PROJECT_OFF_RAMPS.items():
            if project["fields"].get(f"{off_ramp}_reason") or (
                    off_ramp == "parked" and project["fields"].get("parked_until")):
                for field in required:
                    if not project["fields"].get(field):
                        problems.append(f"{ref}: {off_ramp} without {field} (§3.1)")

    for ref, task in store["tasks"].items():
        match = TASK_RE.match(ref)
        if not match:
            problems.append(f"{ref}: not a valid task ref")
            continue
        parent = f"P-{match.group(1)}"
        if parent not in store["projects"]:
            problems.append(f"{ref}: project {parent} is not in the store")
        state = task["fields"].get("state")
        if state not in TASK_STATES + [TASK_OFF_RAMP]:
            problems.append(f"{ref}: unknown state {state!r}")
        if parent == INBOX_PROJECT and state not in (None, "drafted"):
            problems.append(f"{ref}: state {state!r} but project is {INBOX_PROJECT} (§3.2)")
        if state == "approved" and not task["fields"].get("approved_by"):
            advisories.append(f"{ref}: approved with no approved_by (§3.2)")
        if state in ("shipped", TASK_OFF_RAMP) and task["fields"].get("invocation_path"):
            advisories.append(
                f"{ref}: {state} but still carries an invocation -- the chain link "
                f"was either followed and not cleared, or never followed")
        if state in ("shipped", TASK_OFF_RAMP) and task["fields"].get("decision_owed"):
            advisories.append(
                f"{ref}: {state} but still carries a decision_owed -- either the "
                f"decision was taken and nobody cleared it, or it shipped "
                f"without one")
        # The plan, now that it lives here. Checked rather than assumed:
        # TASK-016's deliverable 3 reads these fields and PLAN.md is the only
        # other copy of the ordering, so a wrong row or a dangling dependency
        # has to be visible before anything is retired on the strength of it.
        for row in plan_rows_of(task["fields"]):
            if not (isinstance(row, int) and 1 <= row <= PLAN_ROWS):
                problems.append(f"{ref}: plan_row {row!r} is not one of the "
                                f"{PLAN_ROWS} rows in the plan")
        for dependency in task["fields"].get("depends_on") or []:
            resolved = register.resolve(dependency)
            if resolved == ref:
                problems.append(f"{ref}: depends_on itself")
            elif resolved not in store["tasks"]:
                problems.append(f"{ref}: depends_on {dependency}, which is not "
                                f"in the store")

    # Ordering, checked in a second pass because it needs every task loaded.
    # `depends_on` is the register's copy of "row 8 comes after row 7", and a
    # chain that points the wrong way would be a plan reversed in the one place
    # that is about to become authoritative about it.
    for ref, task in store["tasks"].items():
        rows = [row for row in plan_rows_of(task["fields"]) if isinstance(row, int)]
        for dependency in task["fields"].get("depends_on") or []:
            other = store["tasks"].get(register.resolve(dependency))
            if not other or not rows:
                continue
            other_rows = [row for row in plan_rows_of(other["fields"])
                          if isinstance(row, int)]
            if other_rows and min(other_rows) > min(rows):
                problems.append(
                    f"{ref}: row {min(rows)} depends on {dependency}, which is "
                    f"row {min(other_rows)} -- the plan's order is reversed here")

    view = current_runs(store["runs"])
    for ref, record in view.items():
        if not RUN_RE.match(ref):
            problems.append(f"{ref}: not a valid run ref")
        if register.natural_key(ref) != record.get("claude_session_id"):
            problems.append(f"{ref}: register natural key does not match the record")
        task_ref = record.get("task_ref")
        if task_ref and task_ref not in store["tasks"]:
            advisories.append(f"{ref}: task_ref {task_ref} is not in the store")
    # The session->task link, checked from both ends. It is written twice by
    # necessity -- Task.runs here, and ledger/run-tasks.json which carries the
    # evidence prose the Recorder joins on -- so the one thing that must not
    # happen is the two drifting apart quietly. This is what stops that.
    claimed = {}
    for ref, task in store["tasks"].items():
        for run_ref in task["fields"].get("runs") or []:
            claimed.setdefault(register.resolve(run_ref), []).append(ref)
    for run_ref, task_refs in claimed.items():
        if run_ref not in view:
            problems.append(f"{task_refs[0]}: cites run {run_ref}, which is not in the store")
        elif len(task_refs) > 1:
            problems.append(f"{run_ref} is claimed by {', '.join(task_refs)}; "
                            f"a Run belongs to at most one Task (§2)")
        elif view[run_ref].get("task_ref") not in (None, task_refs[0]):
            problems.append(f"{run_ref}: the Run says task {view[run_ref]['task_ref']}, "
                            f"{task_refs[0]} claims it -- the link disagrees with itself")
    for ref, record in view.items():
        task_ref = record.get("task_ref")
        if task_ref and ref not in claimed and task_ref in store["tasks"]:
            advisories.append(f"{task_ref}: run {ref} links here but is not in its "
                              f"`runs` list")

    # The handoff files at the repo root (§4) mint a ref in their own front
    # matter. Nothing forced them through the allocator, so on 6 September
    # TASK-007-risk-model.md held P-0001-T07 without registering it and the
    # allocator handed the same ref to a later task. Refs are only unique if
    # every ref is visible here, so these are checked too.
    for path in sorted(Path(store["root"]).glob("TASK-*.md")):
        try:
            fields, _ = read_record(path)
        except Exception as problem:                  # a malformed handoff is a problem
            problems.append(f"{path.name}: front matter unreadable ({problem})")
            continue
        ref = fields.get("ref")
        if not ref:
            problems.append(f"{path.name}: no ref in its front matter (§4)")
            continue
        key = register.natural_key(ref)
        if key is None:
            problems.append(f"{path.name}: ref {ref} is not in store/refs.json -- "
                            f"the allocator can hand it to something else")
        elif key != path.name:
            problems.append(f"{path.name}: ref {ref} is registered to {key}")
        elif ref not in store["tasks"]:
            advisories.append(f"{path.name}: ref {ref} has no record in tasks/")

    # §3.1: an override is evidence, and evidence nothing reads is filing. Every
    # overridden gate is surfaced here for as long as it stands, named with who
    # and why, so a project that advanced owing fields cannot go quiet.
    for record in store.get("decisions") or []:
        if not record.get("overridden"):
            continue
        ref = record.get("project_ref") or record.get("task_ref")
        target = record.get("to_stage") or record.get("to_state")
        override = record.get("override") or {}
        owed = ", ".join(debt["field"] for debt in record.get("debts") or [])
        advisories.append(
            f"{ref}: gate to {target!r} was overridden by "
            f"{override.get('by')} on {record.get('at', '')[:10]} -- "
            f"{override.get('reason')} (owed: {owed or 'nothing'})")

    superseded = len(store["runs"]) - len(view)
    if superseded:
        advisories.append(f"{superseded} superseded run record(s) retained (§8.5)")
    return {"problems": problems, "advisories": advisories,
            "counts": {"projects": len(store["projects"]), "tasks": len(store["tasks"]),
                       "runs": len(view), "run_records": len(store["runs"])}}


def export(store, register):
    """Full JSON export of the whole store, no pagination (§8.5).

    Superseded records ship too. An export that showed only the current view
    would quietly disagree with the append-only log it came from."""
    view = current_runs(store["runs"])
    return {
        "exported_at": utc_now(),
        "basis": ("Every cost figure here is a client-side estimate at list rates "
                  "on the subscription rail -- a shadow price, not a bill."),
        "refs": register.data,
        "projects": {ref: project["fields"] for ref, project in store["projects"].items()},
        "tasks": {ref: task["fields"] for ref, task in store["tasks"].items()},
        "runs": list(view.values()),
        "superseded_runs": [record for record in store["runs"]
                            if record is not view.get(record["ref"])],
        "decisions": list(store.get("decisions") or []),
        "disclosive_fields": sorted(DISCLOSIVE_FIELDS),
    }


# --------------------------------------------------------------------------
# Export consent (§8.5). "Full JSON export of the whole store on demand" --
# and row 8's half of that is the *demand*, not the serialiser.
#
# Three things a consent step has to do, and only the third is a gate:
#   1. Say what the export contains, by name and by count.
#   2. Say what it redacted, and what it did NOT.
#   3. Refuse to write a file until someone puts their name to it.
#
# The record goes in `decisions/YYYY-MM.jsonl` beside StageTransition and
# TaskTransition -- §8.5 calls DecisionRecord append-only and an export of the
# whole store in front of a client is a decision, not a read. No ref is
# allocated: §2 gives refs to Projects, Tasks and Runs and gives none to a log
# entry, the same reasoning an earlier session wrote down for transitions.
# --------------------------------------------------------------------------

EXPORT_CONSENT_KIND = "ExportConsent"


class ConsentNotRecorded(StoreError):
    """An export was asked to write a file with no name and no reason on it."""


def disclosive_inventory(payload):
    """How many values in each disclosive field this payload actually carries.

    Enumerative over DISCLOSIVE_FIELDS, exactly as the redactor is, and for the
    same reason: a heuristic scan for things that look like paths would one day
    be wrong in front of a client. Counting the same list the redactor works
    from means the manifest and the redaction cannot disagree about scope --
    if a field is missing from the list, both are blind to it together and
    `governance.audit_presentation` is the thing that catches that.
    """
    counts = {}

    def note(field):
        counts[field] = counts.get(field, 0) + 1

    for bucket in (payload.get("projects") or {}, payload.get("tasks") or {}):
        for fields in bucket.values():
            for field in DISCLOSIVE_FIELDS:
                value = fields.get(field)
                if isinstance(value, str) and value.strip() not in ("", "null"):
                    note(field)
    for record in (list(payload.get("runs") or [])
                   + list(payload.get("superseded_runs") or [])):
        for field in DISCLOSIVE_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                note(field)
    # The allocation register, which is the one that is easy to miss: a ref is
    # bound to a natural key and a project's natural key is its slug. An earlier session
    # found this leak with the audit rather than by reasoning about it, so it
    # is counted here by the same rule the redactor uses -- project and task
    # bindings only. A Run's natural key is a uuid and names nothing.
    for binding in ((payload.get("refs") or {}).get("bindings") or {}).values():
        if isinstance(binding, dict) and binding.get("kind") in ("project", "task"):
            if isinstance(binding.get("natural_key"), str) and binding["natural_key"].strip():
                note("natural_key")
    return dict(sorted(counts.items()))


def export_manifest(payload, presentation_mode=False, audit=None, destination=None,
                    unredacted=None):
    """What this export contains and what it redacted -- the thing a consent
    is consent *to*.

    `unredacted` is the payload before `governance.present()` ran, when there
    is one. It has to be counted rather than the presented copy, because the
    number worth stating is how many disclosive values were in the export
    before redaction -- a presented payload counts zero by construction and
    "zero disclosive values" would read as "there was nothing to hide".
    """
    source = unredacted if unredacted is not None else payload
    inventory = disclosive_inventory(source)
    total = sum(inventory.values())
    warnings = []
    if not presentation_mode and total:
        warnings.append(
            f"{total} disclosive value(s) ship IN THE CLEAR across "
            f"{len(inventory)} field(s). §8.5: paths can be disclosive even when "
            f"contents are not. Re-run through governance.py --present to redact them.")
    if presentation_mode and audit and not audit.get("clean", True):
        warnings.append(
            "PRESENTATION MODE FAILED ITS OWN AUDIT: "
            + ", ".join(audit.get("leaked") or []))
    return {
        "exported_at": payload.get("exported_at"),
        "destination": destination,
        "presentation_mode": bool(presentation_mode),
        "counts": {
            "projects": len(payload.get("projects") or {}),
            "tasks": len(payload.get("tasks") or {}),
            "runs": len(payload.get("runs") or []),
            "superseded_runs": len(payload.get("superseded_runs") or []),
            "decisions": len(payload.get("decisions") or []),
        },
        "disclosive_values": inventory,
        "disclosive_values_total": total,
        "redacted_fields": sorted(DISCLOSIVE_FIELDS) if presentation_mode else [],
        "audit": audit,
        "warnings": warnings,
        "basis": ("Every cost figure in this export is a client-side estimate at "
                  "list rates on the subscription rail -- a shadow price, not money "
                  "that moved. Refs carry no meaning and are never redacted."),
    }


def consent_record(manifest, by=None, reason=None, at=None):
    """The DecisionRecord an export writes. Needs a name and a reason.

    The same shape an earlier session gave an override, and for the same reason: a
    consent that cannot be written down is not one. The manifest is folded in
    whole rather than referenced, because the log has to be readable years
    later without re-running the export that produced it.
    """
    if not (by and str(by).strip()):
        raise ConsentNotRecorded(
            "an export needs a name: pass --consent-by. §8.5's export is on "
            "demand, and a demand with nobody's name on it is not recorded.")
    if not (reason and str(reason).strip()):
        raise ConsentNotRecorded(
            "an export needs a reason: pass --consent-reason. What the export "
            "is for is the half of the record that is worth reading later.")
    audit = manifest.get("audit") or {}
    return {
        "kind": EXPORT_CONSENT_KIND,
        "at": at or utc_now(),
        "by": str(by).strip(),
        "reason": str(reason).strip(),
        "destination": manifest.get("destination"),
        "presentation_mode": manifest.get("presentation_mode"),
        "counts": manifest.get("counts"),
        "disclosive_values_in_the_clear": (
            0 if manifest.get("presentation_mode")
            else manifest.get("disclosive_values_total", 0)),
        "redacted_fields": manifest.get("redacted_fields") or [],
        "audit_clean": audit.get("clean") if audit else None,
        "warnings": manifest.get("warnings") or [],
    }


def format_manifest(manifest):
    """The manifest as lines, for a terminal. One implementation, so the text
    a person reads before consenting is the text the record keeps."""
    lines = []
    mode = "PRESENTED (§8.5 redacted)" if manifest["presentation_mode"] else "IN THE CLEAR"
    lines.append(f"export manifest  --  {mode}")
    lines.append(f"  destination   {manifest.get('destination') or '(stdout, nothing written)'}")
    counts = manifest["counts"]
    lines.append("  contains      " + ", ".join(
        f"{value} {name}" for name, value in counts.items()))
    if manifest["disclosive_values_total"]:
        lines.append(f"  disclosive    {manifest['disclosive_values_total']} value(s): "
                     + ", ".join(f"{field} x{n}"
                                 for field, n in manifest["disclosive_values"].items()))
    else:
        lines.append("  disclosive    none present")
    if manifest["redacted_fields"]:
        lines.append(f"  redacted      {len(manifest['redacted_fields'])} field(s): "
                     + ", ".join(manifest["redacted_fields"]))
    audit = manifest.get("audit")
    if audit:
        lines.append(f"  audit         checked {audit.get('checked')} value(s), "
                     + ("clean" if audit.get("clean")
                        else "LEAKED: " + ", ".join(audit.get("leaked") or [])))
    for warning in manifest["warnings"]:
        lines.append(f"  WARNING       {warning}")
    return lines


def record_export_consent(manifest, by, reason, directory=None, root=None, at=None):
    """Build the record and append it. Raises before anything is written."""
    record = consent_record(manifest, by=by, reason=reason, at=at)
    directory = directory or ((Path(root) / "decisions") if root else DECISIONS_DIR)
    append_decisions([record], directory)
    return record


def export_consents(store_data):
    """Every recorded export, newest last. Evidence nothing reads is filing."""
    return [record for record in (store_data.get("decisions") or [])
            if record.get("kind") == EXPORT_CONSENT_KIND]

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="The entity store (spec §8.3).")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--list", choices=["projects", "tasks", "runs"])
    parser.add_argument("--show")
    parser.add_argument("--ingest-runs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--consent-by",
                        help="who is asking for the export. Required to WRITE one "
                             "(§8.5); printing to stdout writes nothing and is not "
                             "gated.")
    parser.add_argument("--consent-reason",
                        help="what the export is for. Recorded in decisions/ with "
                             "the manifest, append-only.")
    parser.add_argument("--consent-log", action="store_true",
                        help="every export that has been consented to")
    parser.add_argument("--capture", metavar="ONE_LINER",
                        help="capture a project (§3.1: two fields). Allocates a ref "
                             "through refs.json and writes projects/P-NNNN.md.")
    parser.add_argument("--name", help="name for --capture; defaults to the one-liner")
    parser.add_argument("--capture-task", nargs=2, metavar=("P-NNNN", "ONE_LINER"),
                        help="add a task to a project. Allocates the next task ref "
                             "through refs.json, writes tasks/P-NNNN-TNN.md and "
                             "prints the ref. The only way a task ref is made.")
    parser.add_argument("--transition", metavar="REF",
                        help="move a project to --to. Refuses an advancing move that "
                             "owes fields (§3.1); override with --override-by and "
                             "--override-reason, which is recorded, never silent.")
    parser.add_argument("--transition-task", metavar="REF",
                        help="move a task to --to (§3.2).")
    parser.add_argument("--to", help="the stage or state to move to")
    parser.add_argument("--override-by", help="who is overriding the gate")
    parser.add_argument("--override-reason", help="why the gate is being overridden")
    parser.add_argument("--decisions", action="store_true",
                        help="the append-only StageTransition log (§8.5)")
    parser.add_argument("--outcome", metavar="REF",
                        help="append what happened to a task's outcome (§2.2). "
                             "Appended, never replaced; needs --text.")
    parser.add_argument("--text", help="the outcome entry, in words. One "
                                       "paragraph: front matter is one line per "
                                       "field, and a chapter belongs in a document.")
    parser.add_argument("--owed", action="store_true",
                        help="what is waiting on a person: every task carrying "
                             "a decision_owed, with the sentence that says what "
                             "has to be decided. Read this one first.")
    parser.add_argument("--owe", metavar="REF",
                        help="set a task's decision_owed to --text: ONE sentence "
                             "naming what has to be decided. Hand-written, never "
                             "derived.")
    parser.add_argument("--clear", action="store_true",
                        help="with --owe, clear the decision instead of setting "
                             "it. Record the decision as an --outcome first: "
                             "this field is the question, not the answer.")
    parser.add_argument("--plan", action="store_true",
                        help="the eight rows read back out of the register: which "
                             "task claims each, its state, its order and its outcome")
    parser.add_argument("--integrity", action="store_true",
                        help="the append-only tripwire on its own: what runs/, "
                             "decisions/ and risk/ looked like when last seen, "
                             "and what they look like now")
    parser.add_argument("--integrity-update", action="store_true",
                        help="advance the baseline to what is on disk, for every "
                             "file that is clean. A file with a problem is "
                             "refused, which is the point of it")
    parser.add_argument("--integrity-accept", metavar="PATH", action="append",
                        help="advance the baseline over a violation on this path "
                             "anyway. Needs --accept-by and --accept-reason, and "
                             "is recorded in decisions/ append-only. It accepts a "
                             "finding; it does not repair anything")
    parser.add_argument("--accept-by", help="who is accepting the finding")
    parser.add_argument("--accept-reason", help="why it is being accepted")
    parser.add_argument("--out")
    return parser.parse_args(argv)


def _override_from_args(args):
    if not (args.override_by or args.override_reason):
        return None
    return {"by": args.override_by, "reason": args.override_reason}


def _do_transition(args, store_data, register, root, say):
    """The CLI half of enforcement. One path for projects and tasks, because
    the refusal, the override and the append are the same three steps."""
    is_task = bool(args.transition_task)
    ref = register.resolve(args.transition_task or args.transition)
    kind = "tasks" if is_task else "projects"
    if ref not in store_data[kind]:
        say(f"{ref}: not in the store.")
        return 1
    if not args.to:
        say("--to is required: name the stage or state to move to.")
        return 1
    record = store_data[kind][ref]
    fields, body = record["fields"], record["body"]
    override = _override_from_args(args)
    try:
        if is_task:
            updated, notes, transition = transition_task(
                fields, args.to, override=override)
        else:
            updated, transition, _ = transition_project(
                fields, args.to, override=override)
            notes = []
    except StageGateRefused as refused:
        say(f"REFUSED  {refused}")
        for debt in refused.debts:
            say(f"  owes   {format_debt(debt)}")
        say("Nothing was written. Fill the fields, or pass --override-by and "
            "--override-reason; an override is recorded, not silent (§3.1).")
        return 1
    except StoreError as problem:
        say(f"REFUSED  {problem}")
        return 1

    if args.dry_run:
        say(f"would move {ref}  ->  {args.to}")
        for debt in transition["debts"]:
            say(f"  owes   {format_debt(debt)}")
        say("Dry run. Nothing was written.")
        return 0

    writer = write_task_record if is_task else write_record
    try:
        writer(Path(record["path"]), updated, body)
    except AppendOnlyViolation as violation:
        say(f"REFUSED  {ref}: {violation}")
        say("Nothing was written.")
        return 1
    written = append_decisions([transition], root / "decisions")
    say(f"moved    {ref}  ->  {args.to}")
    for note in notes:
        say(f"  note   {note}")
    for debt in transition["debts"]:
        say(f"  owes   {format_debt(debt)}")
    if transition["overridden"]:
        say(f"  OVERRIDDEN by {transition['override']['by']}: "
            f"{transition['override']['reason']}")
    for path, count in written:
        say(f"-> appended {count} to {path}")
    return 0


def _do_owed(store_data, say):
    """`--owed`: the question this register could not answer until an earlier session."""
    owed = decisions_owed(store_data)
    if not owed:
        say("Nothing is waiting on a person.")
        say("That is not the same as nothing being unfinished -- `--list tasks` "
            "is the whole board; this is only what someone has to DECIDE.")
        return 0
    say(f"{len(owed)} decision(s) waiting on a person:")
    say()
    for item in owed:
        say(f"{item['ref']}  {item['state'] or '-'}")
        say(f"  DECIDE   {item['decision_owed']}")
        if item["brief"]:
            say(f"  about    {item['brief']}")
        say(f"  detail   python3 store/store.py --show {item['ref']}")
        say()
    say("Record what you decide with --outcome, then clear it with --owe REF "
        "--clear. The decision belongs in the outcome; this field is only the "
        "question.")
    return 0


def _do_owe(args, store_data, register, say):
    """Set or clear one task's decision_owed."""
    ref = register.resolve(args.owe)
    if ref not in store_data["tasks"]:
        say(f"{ref}: not in the store.")
        return 1
    record = store_data["tasks"][ref]
    try:
        if args.clear:
            updated, warning = clear_decision_owed(record["fields"])
        else:
            if not args.text:
                say("--text is required: a decision owed is one sentence saying "
                    "what has to be decided. Use --clear to say nothing is.")
                return 1
            updated, warning = set_decision_owed(record["fields"], args.text), None
    except StoreError as problem:
        say(f"REFUSED  {problem}")
        return 1
    if args.dry_run:
        say(f"would set {ref}  decision_owed -> {updated['decision_owed']!r}")
        say("Dry run. Nothing was written.")
        return 0
    try:
        # previous=None on purpose, the same reason --outcome does it: the file
        # is re-read so a second window's append refuses here rather than being
        # dropped (P-0001-T20).
        write_task_record(Path(record["path"]), updated, record["body"])
    except AppendOnlyViolation as violation:
        say(f"REFUSED  {ref}: {violation}")
        say("Nothing was written.")
        return 1
    if args.clear:
        say(f"cleared  {ref}")
    else:
        say(f"owed     {ref}  {updated['decision_owed']}")
    if warning:
        say(f"  NOTE   {warning}")
    return 0


def _do_outcome(args, store_data, register, say):
    """Append one outcome entry. §2.2's field, TASK-016."""
    ref = register.resolve(args.outcome)
    if ref not in store_data["tasks"]:
        say(f"{ref}: not in the store.")
        return 1
    if not args.text:
        say("--text is required: an outcome is what happened, in words.")
        return 1
    record = store_data["tasks"][ref]
    try:
        updated, entry = append_outcome(record["fields"], args.text)
    except StoreError as problem:
        say(f"REFUSED  {problem}")
        return 1
    if args.dry_run:
        say(f"would append to {ref}")
        say(f"  {entry}")
        say("Dry run. Nothing was written.")
        return 0
    try:
        # `previous=None` on purpose: write_task_record re-reads the file, so a
        # second window that appended since this store was loaded refuses here
        # instead of losing its entry (PARKED.md item 17).
        write_task_record(Path(record["path"]), updated, record["body"])
    except AppendOnlyViolation as violation:
        say(f"REFUSED  {ref}: {violation}")
        say("Nothing was written. Re-read the record and append to what it says now.")
        return 1
    entries = outcome_entries(updated["outcome"])
    say(f"appended {ref}  (entry {len(entries)} of {len(entries)})")
    say(f"  {entry}")
    return 0


def _do_plan(store_data, say):
    """The read-back. Rows, not narrative."""
    view = plan_view(store_data)
    for row in range(1, PLAN_ROWS + 1):
        tasks = view["rows"][row]
        if not tasks:
            say(f"row {row}  -- no task claims this row")
            continue
        for task in tasks:
            after = ", ".join(task["depends_on"]) or "nothing"
            say(f"row {row}  {task['ref']}  {task['state'] or '?':<9} after {after}")
            for entry in task["outcome"]:
                say(f"          {entry}")
    for row, tasks in sorted(view["stray"].items()):
        say(f"row {row}  NOT ONE OF THE {PLAN_ROWS} ROWS: "
            f"{', '.join(task['ref'] for task in tasks)}")
    if view["unclaimed"]:
        say(f"unclaimed rows: {', '.join(str(row) for row in view['unclaimed'])}")
    if view["without_outcome"]:
        say(f"rows with no outcome written: "
            f"{', '.join(str(row) for row in view['without_outcome'])}")
    if not view["unclaimed"] and not view["stray"]:
        say(f"all {PLAN_ROWS} rows are claimed by a task record.")
    return 0


# --------------------------------------------------------------------------
# The append-only tripwire, on the CLI. store/integrity.py is the check; this
# is how a session hears about it, in the place a session is already looking.
# --------------------------------------------------------------------------

def _integrity_line(finding):
    return (f"integrity {finding['path']}: {finding['verdict']} -- "
            f"{finding['detail']}")


def _say_integrity(root, say, result=None):
    """The one-line summary and every finding. Returns the result so a caller
    can decide what to do about it."""
    config_path = integrity.config_for(root)
    if not config_path.exists():
        say("integrity no baseline in this tree, so nothing is verified here "
            "(store/integrity.json)")
        return None
    try:
        result = result or integrity.verify(root, integrity.load_config(config_path))
    except (OSError, ValueError) as problem:      # the tripwire itself unreadable
        say(f"PROBLEM  integrity: the baseline could not be read ({problem})")
        return None
    coverage = result["coverage"]
    say(f"integrity {coverage['files']} file(s) watched, {coverage['recorded']} "
        f"in the baseline, {coverage['bytes_covered']} of "
        f"{coverage['bytes_on_disk']} bytes verified")
    for finding in result["problems"]:
        say(f"PROBLEM  {_integrity_line(finding)}")
    for finding in result["advisories"]:
        say(f"advisory {_integrity_line(finding)}")
    return result


def _do_integrity(args, root, say):
    stamp = utc_now()
    config_path = integrity.config_for(root)

    def load():
        return integrity.load_config(config_path)

    if args.integrity:
        result = _say_integrity(root, say)
        if result is None:
            return 1
        if not result["coverage"]["recorded"]:
            say("THE BASELINE IS EMPTY, so this verifies nothing yet -- an empty "
                "baseline is not a clean one. --integrity-update records what is "
                "there now, and from then on it is measured rather than believed.")
        elif result["clean"]:
            say("append-only holds: no earlier byte has moved and nothing has "
                "been lost, for every byte in the baseline.")
        say("this reports and it never repairs -- a lost record is a finding, "
            "and the correction is a new record saying so.")
        return 0 if result["clean"] else 1

    accepted = list(args.integrity_accept or [])
    if accepted:
        if not (args.accept_by and args.accept_reason):
            say("PROBLEM  --integrity-accept needs --accept-by and "
                "--accept-reason: accepting a finding is a decision somebody "
                "took, and an unattributed one is not evidence of anything.")
            return 1
        before = integrity.verify(root, load())
        taken = [f for f in before["problems"] if f["path"] in accepted]
        if not taken:
            say(f"nothing to accept on {', '.join(accepted)}: there is no "
                f"finding there. Nothing was recorded and nothing was written.")
            return 1
        append_decisions([{
            "kind": integrity.ACCEPTANCE_KIND,
            "at": stamp,
            "by": args.accept_by,
            "reason": args.accept_reason,
            "paths": sorted(set(accepted)),
            "findings": [{"path": f["path"], "verdict": f["verdict"],
                          "detail": f["detail"]} for f in taken],
        }], root / "decisions")
        for finding in taken:
            say(f"recorded  accepted {finding['verdict']} on {finding['path']} "
                f"({args.accept_by}: {args.accept_reason})")

    config, report = integrity.update_baseline(root, load(), now=stamp,
                                               accepted=accepted)
    integrity.save_config(config, config_path)
    say(f"baseline  {len(report['advanced'])} file(s) advanced, "
        f"{len(config['baseline'])} in the baseline now")
    for relative in report["advanced"]:
        say(f"          {relative}")
    for refusal in report["refused"]:
        say(f"REFUSED  {refusal['path']}: {', '.join(refusal['verdicts'])} -- "
            f"the baseline does not move over a finding. If it did, the check "
            f"would report this once and then agree with it forever. Record the "
            f"finding, then --integrity-accept it with a name and a reason.")
    return 1 if report["refused"] else 0


def main(argv=None, out=None):
    args = parse_args(argv)
    out = out or sys.stdout
    root = Path(args.root)

    def say(line=""):
        print(line, file=out)

    # The tripwire runs BEFORE the store loads, and the ordering is the whole
    # point. An interrupted write stops load_store() dead, and a traceback is
    # not a verdict -- it names no failure and it takes every other check down
    # with it. So the integrity commands never load the store at all, and the
    # ones that do are ready to be told why they could not.
    if args.integrity or args.integrity_update or args.integrity_accept:
        return _do_integrity(args, root, say)

    register = RefRegister.load(root / "store" / "refs.json")
    try:
        store = load_store(root)
    except InterruptedWrite as problem:
        say(f"PROBLEM  {problem}")
        _say_integrity(root, say)
        say("the store could not be loaded, so nothing else here ran. This is a "
            "finding to record, not a file to repair (§8.5).")
        return 1

    if args.owed:
        return _do_owed(store, say)

    if args.owe:
        return _do_owe(args, store, register, say)

    if args.outcome:
        return _do_outcome(args, store, register, say)

    if args.plan:
        return _do_plan(store, say)

    if args.ingest_runs:
        ledger = json.loads((root / "ledger" / "runs.json").read_text(encoding="utf-8"))
        ledger_runs = ledger["runs"] if isinstance(ledger, dict) else ledger
        records, report = ingest_runs(ledger_runs, register, store["runs"])
        for record in report["new"]:
            say(f"new        {record['ref']}  {record['session'][:8]}  "
                f"${record['cost_usd']:.2f}")
        for record in report["superseded"]:
            say(f"supersedes {record['ref']}  ${record['from_cost_usd']:.2f} -> "
                f"${record['to_cost_usd']:.2f}  ({', '.join(record['changed'])})")
        say(f"unchanged  {len(report['unchanged'])}")
        if args.dry_run:
            say("Dry run. Nothing was written.")
            return 0
        by_file = {}
        for record in records:
            by_file.setdefault(runs_path(record["started_at"], root / "runs"), []).append(record)
        for path, group in by_file.items():
            append_runs(path, group)
            say(f"-> appended {len(group)} to {path}")
        register.save()
        say(f"-> refs {register.path}")
        return 0

    if args.transition or args.transition_task:
        return _do_transition(args, store, register, root, say)

    if args.decisions:
        for record in store.get("decisions") or []:
            ref = record.get("project_ref") or record.get("task_ref")
            target = record.get("to_stage") or record.get("to_state")
            mark = "OVERRIDDEN" if record.get("overridden") else (
                "enforced" if record.get("enforced") else "advisory")
            say(f"{record.get('at')}  {ref:<14} {record.get('from_stage') or record.get('from_state')}"
                f" -> {target:<12} {mark}")
            if record.get("overridden"):
                say(f"{'':22}by {record['override']['by']}: {record['override']['reason']}")
        if not store.get("decisions"):
            say("No transitions recorded. The log is written by --transition; "
                "it is append-only and a correction is a new record (§8.5).")
        return 0

    if args.capture:
        ref, path, debts, created = capture_project(
            args.capture, register, name=args.name, root=root)
        if not created:
            say(f"exists     {ref}  {path}")
            say("Already captured under this name. Refs are allocated once and never "
                "reused, so nothing was written.")
        else:
            register.save()
            say(f"captured   {ref}  {path}")
            say(f"-> refs {register.path}")
        for debt in debts:
            say(f"advisory   {ref}: stage 'captured' owes {debt['field']!r} "
                f"(from {debt['owed_from']!r}: {debt['exit_test']})")
        say("Advisory only. §3.1 makes fields mandatory as a project advances, never "
            "at creation; enforcement is an earlier session.")
        return 0

    if args.capture_task:
        project_ref, one_liner = args.capture_task
        try:
            ref, path, created = capture_task(project_ref, one_liner, register, root=root)
        except (StoreError, RefCollision, AppendOnlyViolation) as problem:
            say(f"REFUSED  {problem}")
            say("Nothing was written.")
            return 1
        if not created:
            say(f"exists     {ref}  {path}")
            say("A task with this wording already has this ref. Nothing was written.")
            return 1
        register.save()
        say(ref)
        say(f"captured   {ref}  {path}")
        say(f"-> refs {register.path}")
        return 0

    if args.consent_log:
        consents = export_consents(store)
        if not consents:
            say("No export has been consented to. §8.5's export is on demand and "
                "the demand is recorded; nothing has demanded one yet.")
            return 0
        for record in consents:
            say(f"{record['at']}  {record['by']}  "
                f"{'presented' if record.get('presentation_mode') else 'IN THE CLEAR'}  "
                f"-> {record.get('destination') or '(stdout)'}")
            say(f"    {record.get('reason')}")
            if record.get("disclosive_values_in_the_clear"):
                say(f"    {record['disclosive_values_in_the_clear']} disclosive "
                    f"value(s) left unredacted")
        return 0

    if args.export:
        payload = export(store, register)
        manifest = export_manifest(payload, destination=args.out)
        for line in format_manifest(manifest):
            say(line)
        if not args.out:
            # Nothing is written, so nothing is gated. The manifest still
            # prints: a person about to pipe this somewhere should see what is
            # in it before the pipe, not after.
            say(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        try:
            record = record_export_consent(manifest, args.consent_by,
                                           args.consent_reason, root=root)
        except ConsentNotRecorded as refused:
            say(f"REFUSED  {refused}")
            say("Nothing was written. §8.5's export is on demand; row 8 is the "
                "demand being recorded rather than assumed.")
            return 1
        Path(args.out).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        say(f"-> {args.out}")
        say(f"consent recorded  {record['at']}  {record['by']}  "
            f"-> decisions/{record['at'][:7]}.jsonl")
        return 0

    if args.list:
        if args.list == "runs":
            for ref, record in sorted(current_runs(store["runs"]).items()):
                say(f"{ref}  {record.get('task_ref') or '-':<12} "
                    f"${record.get('cost_usd') or 0:>7.2f}  "
                    f"{', '.join(record.get('aliases', [])) or '-'}")
        else:
            for ref, record in sorted(store[args.list].items()):
                fields = record["fields"]
                # The OWED column, an earlier session. `--list` was scannable for state
                # and unreadable for what to DO, which is what sent the owner to
                # line 72 of a task body looking for a question.
                owed = "OWED " if fields.get("decision_owed") else "     "
                say(f"{ref}  {fields.get('stage') or fields.get('state') or '-':<10} "
                    f"{owed}{fields.get('name') or fields.get('brief') or ''}")
        return 0

    if args.show:
        ref = register.resolve(args.show)
        for kind in ("projects", "tasks"):
            if ref in store[kind]:
                say(Path(store[kind][ref]["path"]).read_text(encoding="utf-8").rstrip())
                return 0
        view = current_runs(store["runs"])
        if ref in view:
            say(json.dumps(view[ref], indent=2, sort_keys=True))
            return 0
        say(f"{args.show}: not in the store.")
        return 1

    result = check(store, register)
    counts = result["counts"]
    say(f"store    {counts['projects']} projects, {counts['tasks']} tasks, "
        f"{counts['runs']} runs ({counts['run_records']} records)")
    # §8.5, an earlier session: the store checking itself was never checking that the
    # records it read were all the records there had been.
    tripwire = _say_integrity(root, say)
    for problem in result["problems"]:
        say(f"PROBLEM  {problem}")
    for advisory in result["advisories"]:
        say(f"advisory {advisory}")
    owed = decisions_owed(store)
    if owed:
        say(f"owed     {len(owed)} decision(s) waiting on a person: "
            f"{', '.join(item['ref'] for item in owed)}  (--owed)")
    if not result["problems"] and (tripwire is None or tripwire["clean"]):
        say("no problems. Advisories are what a stage owes, not a refusal (§3.1).")
    return 1 if result["problems"] or (tripwire and not tripwire["clean"]) else 0


if __name__ == "__main__":
    sys.exit(main())
