#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Usage: python3 wrapper/launch.py TASK-003-wrapper.md [--execute]

Turns a task file into one correctly-formed `claude -p` invocation, after the
gate has allowed it. Spec-v0.2.md 7: "Launcher. Turns typed intent into one
correctly-formed command with the spec file attached. Roughly fifty lines."

What this deliberately does NOT do, because Claude Code already does it and
does it correctly: turn management, permission handling, tool dispatch, token
accounting. Cost and the session id are read back from --output-format json.
Output discovery reads the record, not the disk, and belongs to the Recorder.

Three safety properties, in the order they will bite:

  1. --dry-run is the default. Printing the command is the safe path; running
     it costs real tokens and needs --execute.
  2. It refuses to execute when it cannot find the `claude` binary. There is no
     CLI on this machine (verified 5 September 2026), and the failure this
     guards against is the Haiku spec's: an invented command line that reported
     runs complete at 0.00, a meter that fails silently and flatters you.
  3. It detects the rail and hands it to the gate as a field. Per 5.3, scripted
     -p and --bare never read OAuth credentials and require ANTHROPIC_API_KEY,
     so scripting a run for reproducibility silently moves it onto the paid API
     rail at list price. Since TASK-012 (6 September 2026) that is a BLOCK, not
     a warning and not a downgrade to ask: it passes only with a per-run opt-in
     hand-written on the task file, and even then it resolves to an ask. The
     detection lives here because this is where the environment is; the answer
     lives in gate.py, which stays pure. Nothing is written to
     ledger/rails.json: that file is bounded at 2026-09-04 on purpose, so a
     scripted run has to earn its own rule, with a person's hand on it.

     THE LIMIT, STATED: this guards runs that go through this launcher. It
     cannot stop `claude -p --bare` typed by hand. See wrapper/NOTES.md.

No third-party dependencies, no network of its own.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"

# CORRECTED 7 September 2026, by the owner, who said he had the CLI installed and
# was right. The comment here read "Checked 5 September 2026: none of these
# exists on this machine" -- true when written, false from 06:32 on 6 September
# when `claude` 2.1.263 was installed at ~/.local/bin/claude, which is the
# second entry in this very list. `find_claude_binary` returns it today.
#
# The finder's own docstring below has known this since 6 September. This
# comment did not, and nothing reconciled them: a dated observation went stale
# a hundred lines above the code that had already noticed. THE LESSON IS THE
# ONE THIS REGISTER KEEPS RELEARNING -- a fact with a date on it needs a
# re-check, not just a date.
FALLBACK_BINARY_PATHS = (
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

DECISIONS_DIR = HERE.parent / "decisions"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ASK = 2
EXIT_BLOCK = 3


class FrontMatterError(Exception):
    """The task file's front matter is missing or malformed. Loud, because the
    gate is about to act on what is in it."""


# --------------------------------------------------------- front matter ----

def parse_front_matter(text):
    """Parse the flat `key: value` YAML front matter used by the task files.

    Stdlib only and deliberately narrow: the shape in TASK-001, TASK-002 and
    TASK-003 is flat scalars between two `---` fences and nothing else. Lists,
    nesting and multi-line values are rejected rather than half-parsed, because
    a front matter parser that guesses is a way to misread est_p95_usd.

    Returns (front_matter dict, body str)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontMatterError("No front matter: the file does not open with '---'.")

    front = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return front, "\n".join(lines[index + 1:])
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            raise FrontMatterError(
                f"Front matter line {index + 1} is indented; only flat "
                f"'key: value' pairs are supported: {line!r}")
        if ":" not in line:
            raise FrontMatterError(
                f"Front matter line {index + 1} is not 'key: value': {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            raise FrontMatterError(f"Front matter line {index + 1} has an empty key.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in front:
            raise FrontMatterError(f"Front matter key '{key}' appears twice.")
        front[key] = value
    raise FrontMatterError("Front matter is not closed by a second '---'.")


def read_task(path):
    path = Path(path)
    if not path.exists():
        raise FrontMatterError(f"No task file at {path}.")
    return parse_front_matter(path.read_text())


# ---------------------------------------------------------------- rail ----

def detect_rail(env=None):
    """Which rail this invocation would bill, and the warning that goes with it.

    Returns (rail, warning). Subscription and API credit are separate rails,
    not one balance (5.3), so tokens-per-pound mixes two currencies without
    this. Detection is by ANTHROPIC_API_KEY because that is the mechanism: bare
    mode does not read OAuth credentials and requires the key."""
    env = os.environ if env is None else env
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return "subscription", None
    return "api", (
        "ANTHROPIC_API_KEY is set in this environment. A scripted `claude -p` run "
        "does not read OAuth credentials, so this would bill API credit at list "
        "price rather than the subscription (spec 5.3). The tokens look identical "
        "afterwards. If you mean to run on this rail, add a rule to "
        "ledger/rails.json yourself with the date and the reason -- this launcher "
        "will not write one for you.")


# ------------------------------------------------------------- binary ----

def find_claude_binary(env=None, which=None, candidates=None):
    """Locate the `claude` executable, or None.

    None is a real answer, and the caller must refuse to execute on it rather
    than construct a command it cannot verify.

    `candidates` exists so a test can state "there is no binary" instead of
    relying on there not being one. Until 6 September there was no `claude` on
    this machine and the wrapper tests were safe by accident; when one was
    installed, a test that asserted the refusal path executed the real binary
    instead. Absence must be injected, never assumed."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    candidates = FALLBACK_BINARY_PATHS if candidates is None else candidates
    override = (env.get("CLAUDE_CLI") or "").strip()
    if override:
        found = which(override)
        if found:
            return found
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(str(path), os.X_OK) else None
    found = which("claude")
    if found:
        return found
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(str(path), os.X_OK):
            return str(path)
    return None


# ------------------------------------------------------------ command ----

def build_command(binary, spec_path, prompt, *, allowed_tools, permission_mode,
                  output_format="json", session_id=None):
    """The 7 command, as argv.

    `claude -p "$(cat spec)" --allowedTools ... --permission-mode ...
     --output-format json`

    Two details from 7 that this gets right on purpose:
      - the flag is --session-id <uuid>, not --session <run_id>. The invented
        form is the seed the later spec's whole imaginary command line grew from.
      - --permission-mode is always passed. A -p session starts in Manual on
        every plan, so an unattended run without it refuses the Write and Edit
        calls the task depends on, and the failure is quiet."""
    if not binary:
        raise ValueError("build_command needs a real binary path.")
    argv = [binary, "-p", prompt,
            "--allowedTools", ",".join(allowed_tools),
            "--permission-mode", permission_mode,
            "--output-format", output_format]
    if session_id:
        argv += ["--session-id", str(session_id)]
    return argv


def render_command(argv, spec_path):
    """The command as a person would type it, with the prompt shown as
    "$(cat <spec>)" instead of the file's whole text. Printed, never executed:
    execution uses argv directly, so nothing here can change what runs."""
    parts = []
    for index, item in enumerate(argv):
        if index == 2:
            parts.append(f'"$(cat {spec_path})"')
        elif any(ch in item for ch in ' \t"\''):
            parts.append(json.dumps(item))
        else:
            parts.append(item)
    return " ".join(parts)


# ---------------------------------------------------------- run record ----

def run_record(*, task_path, front_matter, decision, rail, session_id, command,
               result=None, now=None):
    """The record of one launch. Deliberately thin: the Recorder (Phase 1
    an earlier session) owns Run and Output records read back from the transcripts. This
    holds only what the launcher alone knows -- what it decided, on which rail,
    and with which session id -- plus total_cost_usd exactly as Claude Code
    reported it. Nothing here counts a token."""
    now = now or datetime.now(timezone.utc)
    record = {
        "task_ref": front_matter.get("ref"),
        "task_file": str(task_path),
        # Carried so the Recorder can derive gate.json's priors from Run
        # records without re-reading the task file, which may have moved or
        # changed since. Same resolution order gate.py uses.
        "task_class": front_matter.get("task_class") or front_matter.get("task_type"),
        "launched_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "rail": rail,
        "rail_basis": ("ANTHROPIC_API_KEY set at launch" if rail == "api"
                       else "no ANTHROPIC_API_KEY at launch; OAuth subscription assumed"),
        "gate": decision.as_dict(),
        "command": command,
        "total_cost_usd": None,
        "cost_source": None,
    }
    if result is not None:
        record["total_cost_usd"] = result.get("total_cost_usd")
        record["cost_source"] = "claude --output-format json"
        returned = result.get("session_id")
        record["session_id_returned"] = returned
        if returned and session_id and returned != session_id:
            record["session_id_mismatch"] = True
    return record


# ---------------------------------------------------------------- CLI ----

def record_api_rail_optin(front_matter, decision, *, rail,
                          directory=None, now=None):
    """Append the consumed API-rail opt-in to decisions/*.jsonl.

    Reuses store.append_decisions rather than inventing a second decisions log:
    §8.5's log is append-only, grouped by month, and a correction is a new
    record. The import is deferred because store.py imports this module for its
    front-matter parsing, and a top-level import here would close that circle.

    Written whenever the gate consumes a complete opt-in -- including on a dry
    run. The log answers "when was API-rail consent exercised, and with what
    cap", and a dry run that consumed the consent is part of that history. It is
    append-only, so nothing is lost by recording too much and something is lost
    by recording too little."""
    optin = decision.api_rail_optin
    if not optin:
        return []
    sys.path.insert(0, str(HERE.parent / "store"))
    import store  # noqa: E402 -- deferred, see above

    # Both spellings are in use: `project_ref: P-0001` in tasks/, and
    # `project: P-0001 (P2P Register)` in the hand-written task briefs. Take the
    # first token of either, and tolerate neither being there.
    project = (front_matter.get("project_ref") or front_matter.get("project") or "").split()
    record = {
        "kind": "ApiRailOptIn",
        "task_ref": front_matter.get("ref") or None,
        "project_ref": project[0] if project else None,
        "at": now or store.utc_now(),
        "rail": rail,
        "gate_action": decision.action,
        # The consent goes in store.py's own override shape -- {by, reason} --
        # rather than in fields of its own. governance.py's _redact_decision
        # already drops `override.reason` whole and keeps who and when, which
        # is exactly right here: the reason is free text a person wrote about a
        # spend, and a name in an export is the one thing §8.5 keeps. Reusing
        # the shape means presentation needs no change to cover this record.
        "override": {"by": optin["allowed_by"], "reason": optin["reason"]},
        "cap_usd": optin["cap_usd"],
        "ceiling_usd": optin["ceiling_usd"],
        "estimate_usd": optin["estimate_usd"],
        # The task file's path is deliberately NOT carried. `task_ref` names the
        # task and a ref carries no meaning; a path is disclosive (§8.5) and
        # decision records are not scanned for secrets by the presentation
        # audit, so the safe move is not to put one here.
        "notes": ["per-run opt-in under §5.3 / TASK-012; an opt-in resolves to "
                  "ask, never allow"],
    }
    return store.append_decisions([record], directory or DECISIONS_DIR)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Gate and launch one Claude Code run from a task file.")
    parser.add_argument("task", help="Path to the task markdown file.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run it. Costs real tokens. Default is --dry-run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the command and stop. The default.")
    parser.add_argument("--allowedTools", dest="allowed_tools", default=None,
                        help="Comma-separated. Default from gate.json.")
    parser.add_argument("--permission-mode", dest="permission_mode", default=None,
                        help="Default from gate.json. Always passed to claude.")
    parser.add_argument("--scheduled", action="store_true",
                        help="This run was approved when it was scheduled (5.5).")
    parser.add_argument("--gate-config", default=None,
                        help="Path to gate.json. Default: beside this script.")
    return parser.parse_args(argv)


def main(argv=None, env=None, out=None, decisions_dir=None):
    args = parse_args(argv)
    out = out or sys.stdout
    env = os.environ if env is None else env

    def say(line=""):
        print(line, file=out)

    try:
        config = gate.load_config(Path(args.gate_config) if args.gate_config else gate.GATE_PATH)
    except gate.GateConfigError as exc:
        say(f"ERROR: {exc}")
        return EXIT_ERROR

    task_path = Path(args.task)
    try:
        front, _body = read_task(task_path)
    except FrontMatterError as exc:
        say(f"ERROR: {exc}")
        return EXIT_ERROR

    defaults = config.get("defaults") or {}
    allowed_tools = ([t.strip() for t in args.allowed_tools.split(",") if t.strip()]
                     if args.allowed_tools else list(defaults.get("allowed_tools") or []))
    permission_mode = args.permission_mode or defaults.get("permission_mode")
    output_format = defaults.get("output_format", "json")
    if not allowed_tools or not permission_mode:
        say("ERROR: allowed_tools and permission_mode must be set, in gate.json "
            "defaults or on the command line. A -p session starts in Manual and "
            "refuses Write and Edit quietly (spec 7).")
        return EXIT_ERROR

    rail, rail_warning = detect_rail(env)
    decision = gate.decide(front, config, scheduled=args.scheduled, rail=rail)

    say(f"task     {front.get('ref', '(no ref)')}  {task_path}")
    say(f"state    {front.get('state', 'unset')}")
    estimate = decision.estimate
    shown = f"${estimate.usd:.2f}" if estimate.usd is not None else "none"
    say(f"estimate {shown}  (source: {estimate.source}) -- {estimate.basis}")
    say(f"gate     {decision.action.upper()} against ${decision.threshold_usd:.2f} in gate.json")
    for reason in decision.reasons:
        say(f"         - {reason}")
    say(f"rail     {rail}")
    if rail_warning:
        say("")
        say("*** RAIL WARNING ***")
        for line in rail_warning.split(". "):
            if line.strip():
                say(f"    {line.strip().rstrip('.')}.")
        say("")

    try:
        for path, count in record_api_rail_optin(
                front, decision, rail=rail, directory=decisions_dir):
            say(f"         opt-in recorded: appended {count} to {path}")
    except Exception as exc:  # the log is the point of the control
        say(f"ERROR: could not record the API-rail opt-in: {exc}")
        return EXIT_ERROR

    session_id = str(uuid.uuid4())
    binary = find_claude_binary(env)
    spec_display = task_path
    if binary:
        command = build_command(binary, spec_display, task_path.read_text(),
                                allowed_tools=allowed_tools,
                                permission_mode=permission_mode,
                                output_format=output_format,
                                session_id=session_id)
        say("command  " + render_command(command, spec_display))
    else:
        # No binary. Show the SHAPE so a dry run is still worth running on this
        # machine, and label it unverified in the same breath. What must not
        # happen is executing an unverified command line -- see the refusal
        # below -- not printing one a person can read.
        command = None
        shape = build_command("claude", spec_display, "", allowed_tools=allowed_tools,
                             permission_mode=permission_mode,
                             output_format=output_format, session_id=session_id)
        say("shape    " + render_command(shape, spec_display))
        say("         UNVERIFIED. No `claude` executable on PATH or in "
            + ", ".join(FALLBACK_BINARY_PATHS) + ".")
        say("         This is the shape the launcher would build, not a command it "
            "has checked.")
        say("         Set CLAUDE_CLI to its path if it is installed elsewhere.")

    if decision.action == gate.BLOCK:
        say("\nBLOCKED. Nothing was run.")
        return EXIT_BLOCK
    if decision.action == gate.ASK:
        say("\nASK. Nothing was run. Answer the points above, then re-run "
            "with --execute once they are settled.")
        return EXIT_ASK

    if args.dry_run and args.execute:
        # Contradictory flags. The safe one wins, loudly: on a tool whose whole
        # posture is "dry run is the default", the unsafe flag must never be the
        # one that silently takes precedence.
        say("\nNOTE: --dry-run and --execute were both given. --dry-run wins.")
    if args.dry_run or not args.execute:
        say("\nDry run. Nothing was run. Add --execute to spend tokens on it.")
        return EXIT_OK

    if not binary:
        say("\nREFUSING TO EXECUTE: no `claude` binary was found, so there is no "
            "command to run.")
        say("A launcher that invents a command line reports runs complete at "
            "0.00 and the meter fails silently (spec 7). Install the CLI "
            "deliberately -- and read 5.3 first, because a scripted -p run bills "
            "the API rail.")
        return EXIT_ERROR

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        say(f"\nclaude exited {completed.returncode}. stderr:\n{completed.stderr}")
        return EXIT_ERROR
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        say("\nclaude returned output that is not JSON. Not parsing cost out of it "
            "by hand -- that is the token accounting this wrapper refuses to "
            "re-implement (spec 7). Raw output:\n" + completed.stdout[:2000])
        return EXIT_ERROR

    # A ZERO-COST SUCCESS IS THE FAILURE THIS LAUNCHER EXISTS TO PREVENT, and
    # until 10 September 2026 it would have sailed through. Measured that day
    # in a clean environment, `claude -p` with no credentials exits 0 and
    # returns {"is_error": true, "total_cost_usd": 0, "result": "Not logged in
    # - Please run /login"}. Every check above passes: the process succeeded
    # and the output is JSON. The record written would have said a session
    # completed for nothing -- "a meter that fails silently and flatters you",
    # which is this file's own words about the failure it guards against.
    if result.get("is_error"):
        say("\nclaude reported an error and nothing usable was produced:")
        say("    " + str(result.get("result") or "(no reason given)"))
        say("No run record was written. A record saying a session completed at "
            "$0.00 is worse than no record.")
        return EXIT_ERROR

    RUNS_DIR.mkdir(exist_ok=True)
    record = run_record(task_path=task_path, front_matter=front, decision=decision,
                        rail=rail, session_id=session_id,
                        command=render_command(command, spec_display), result=result)
    record_path = RUNS_DIR / f"{record.get('session_id_returned') or session_id}.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    cost = record["total_cost_usd"]
    say(f"\ncost     {'$%.2f' % cost if isinstance(cost, (int, float)) else 'not reported'}"
        f"  (from --output-format json, not counted here)")
    if record.get("session_id_mismatch"):
        say("WARNING: claude returned a different session id from the one passed to "
            "--session-id. Recorded both.")
    say(f"-> {record_path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
