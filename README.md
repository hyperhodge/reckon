# Reckon

[![tests](https://github.com/hyperhodge/reckon/actions/workflows/tests.yml/badge.svg)](https://github.com/hyperhodge/reckon/actions/workflows/tests.yml)

**Reckon creates a log of all the work you do in Claude Code (the AI coding assistant) and estimates
its token cost before it starts.**

Built during August and September 2026. I use this build myself, and I am publishing this redacted
core for others to review and try out. I may or may not come back to update or maintain it, but this
snapshot is out there.

## Quickstart

```
$ pip install -e .
```

installs the one command, `reckon`, over `estimate`, `gate`, `record` and `report`. See
[QUICKSTART.md](docs/QUICKSTART.md) for the install and every command's actual captured output,
run end to end on this repository's own sample.

## Why

AI coding and other generative AI tasks are notoriously difficult to size before they start, and
they can be costly to complete. Reckon answers three questions that Claude Code's own interface does
not:

1. **Should this piece of work start at all, at this size?** A task is priced before it runs, and
   a gate allows it, asks first, or blocks it.
2. **What did that session cost?** The Recorder reads the session's own transcript and prices every
   message at list rates. It is an estimate at list prices, not a bill.
3. **Were the estimates any good?** Every estimate is logged beside its actual, and the estimator
   is tested against that history.

It also supports compliance and risk management. Organisations deploying AI tools need to capture
their AI use cases, and Reckon records each piece of work as it happens: what it was for, what data
it touched, whether it is an AI system and in what role, who approved it, and what it cost.

## What this is not

- **Not an audited compliance tool.** It supports the record-keeping compliance work needs; nothing
  here has been reviewed or certified against a compliance framework.
- **Not a bill.** Every figure is a client-side estimate at list rates, from the transcript's own
  token counts. What an account is actually charged can differ, and Claude on the web, the desktop
  app or a phone leaves no transcript on this computer, so it is not counted.
- **Not maintained on a schedule.** It is a snapshot, published as-is; it may or may not be updated.
- **Not a general cost tracker.** Claude Code sessions only, read from local transcripts on this
  computer -- nothing else, and no other tool.
- **The cost gate stands in front of scripted tasks only.** It does not stop or price a conversation
  as you type in it.
- **The estimator's ranges are wide.** The top of a typical range was about ten times the bottom, so
  its predictions are not yet ready to rely on.
- **Headroom against the usage limits is inferred** from the points where requests were refused, not
  read from the account.

## What is in this package

| Folder | What it does |
|---|---|
| `reckon/` | The one command, over the four modules below: `reckon estimate`, `reckon gate`, `reckon record`, `reckon report`. Each subcommand calls the existing module's own function; no logic is copied into it. |
| `store/` | The data model. Projects, Tasks, Runs and Decisions are kept as plain Markdown and JSON files, with no database. Each record gets a permanent reference, and the append-only logs are checked for edits. It also holds presentation mode, which redacts anything identifying before records are shared, and the names check, which stops a file naming a client from being shared. |
| `wrapper/` | The cost gate and the launcher. Before a scripted task runs, the gate prices its brief against past costs for that kind of task and answers allow, ask or block. The launcher builds the one command that runs the task. |
| `estimator/` | Predicts the size of a piece of work before it starts. It reads the proposal, predicts how many assistant messages the work will take, turns that into a cost range using past sessions, and reports how often its past ranges contained the real figure. `model.json` holds the fitted numbers. |
| `ledger/` | Measures what sessions cost. It reads Claude Code's local transcripts, prices each message's tokens at published list rates from `prices.json`, turns one session into a Run record, and estimates how close usage is to the five-hour and weekly limits. |
| `sample/` | A small sample of real records, redacted: every person reads `[Approver]` and free text is replaced. `tasks/` holds copies of three of those sample tasks with their state changed to `approved`, so the gate table below is the real gate's own answer rather than a rule about the shipped, unapproved originals. `session/` holds one invented Claude Code session, marked as such, so `reckon record` has something to run on a fresh install. `SAMPLE.md` shows the estimator and the gate working on all of it. |
| `docs/` | The quickstart, getting started and settings, the data model, and the method. |
| `writing/` | A sample of findings to date. |

## `reckon estimate` and `reckon gate`

```
$ reckon estimate "Add a --json flag to an existing CLI subcommand, with a test."
PROPOSAL
  Add a --json flag to an existing CLI subcommand, with a test.

BROKEN INTO 2 PIECE(S)
  $  8.14   29.8 turns  Add a --json flag to an existing CLI subcommand
  $  8.14   29.8 turns  with a test

COST, AS A RANGE, NOT A POINT
  low   $    4.89   (24 turns, 1 session(s))
  p50   $   16.28   (57 turns, 1 session(s))
  high  $   52.45   (127 turns, 2 session(s))

HIT RATE OF THIS KIND OF RANGE: 78%
  21 of 27 leave-one-out predictions contained the actual turn count inside the stated range.

  A client-side estimate at list rates and a floor on what the account loses. Not a bill. Not sterling.
```

```
$ reckon gate sample/tasks/P-0001-T08.md
task     P-0001-T08  sample/tasks/P-0001-T08.md
state    approved
estimate $14.00  (source: front_matter) -- est_p95_usd from the task file's front matter
gate     ASK against $10.00 in gate.json
         - estimated p95 $14.00 is above the $10.00 threshold in gate.json (est_p95_usd from the task file's front matter).
rail     subscription
command  /tmp/reckon-bin/claude -p "$(cat sample/tasks/P-0001-T08.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id baba21c7-5ae8-4684-abcf-fa62e8227857

ASK. Nothing was run. Answer the points above, then re-run with --execute once they are settled.
```

`reckon record` and `reckon report`, and the other two `reckon gate` answers, are captured in full
in [QUICKSTART.md](docs/QUICKSTART.md).

## Where it could go next

- **The work-breakdown rule is a punctuation split, not a reading of the proposal** — see
  [The method](docs/METHOD.md) §5 for exactly how it works today. Understanding what a proposal is
  actually asking for, rather than splitting on commas and dashes, is a current area of focus.
- Give the estimator a breakdown of each piece of work to size, instead of a one-line title.
- Put the gate in front of conversational sessions, not only scripted tasks.
- Count work done outside Claude Code.

## Tests

Python 3.9 or later, standard library only, no network access. From the top of the repository:

```
python -m unittest discover -s store/tests
python -m unittest discover -s wrapper/tests
python -m unittest discover -s estimator/tests
python -m unittest discover -s ledger/tests
python -m unittest discover -s reckon/tests
```

https://github.com/hyperhodge/reckon/actions runs the same five suites on every push -- that is the badge at the top of
this page.

These are the tests the authors used while building Reckon. They are by no means exhaustive, and
your own use may need more; your mileage may vary.

## Read these

- [Quickstart](docs/QUICKSTART.md) — install, then every command, with its actual captured output.
- [Getting started and settings](docs/SETTINGS.md) — every setting, the file it lives in, its value
  and how to change it.
- [The data model](docs/DATA-MODEL.md) — the records, and the rules that help to maintain their
  integrity.
- [The method](docs/METHOD.md) — estimate, gate, measure, compare.
- [A sample of findings to date](writing/what-the-register-measured.md)

## Licence

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
