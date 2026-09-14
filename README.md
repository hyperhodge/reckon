# Reckon

**A record of work done with Claude, the AI coding assistant: what was asked, what it was estimated
to cost before it started, what it cost, and what came of it.**

Built during August and September 2026. I use this build myself, and I am publishing this redacted
core for others to review and try out. I may or may not come back to update or maintain it, but this
snapshot is out there.

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

## What is in this package

| Folder | What it does |
|---|---|
| `store/` | The data model. Projects, Tasks, Runs and Decisions are kept as plain Markdown and JSON files, with no database. Each record gets a permanent reference, and the append-only logs are checked for edits. It also holds presentation mode, which redacts anything identifying before records are shared, and the names check, which stops a file naming a client from being shared. |
| `wrapper/` | The cost gate and the launcher. Before a scripted task runs, the gate prices its brief against past costs for that kind of task and answers allow, ask or block. The launcher builds the one command that runs the task. |
| `estimator/` | Predicts the size of a piece of work before it starts. It reads the proposal, predicts how many assistant messages the work will take, turns that into a cost range using past sessions, and reports how often its past ranges contained the real figure. `model.json` holds the fitted numbers. |
| `ledger/` | Measures what sessions cost. It reads Claude Code's local transcripts, prices each message's tokens at published list rates from `prices.json`, turns one session into a Run record, and estimates how close usage is to the five-hour and weekly limits. |
| `sample/` | A small sample of real records, redacted: every person reads `[Approver]` and free text is replaced. `SAMPLE.md` shows the estimator and the gate working on them. |
| `docs/` | Getting started and settings, the data model, and the method. |
| `writing/` | A sample of findings to date. |

## Read these

- [Getting started and settings](docs/SETTINGS.md) — every setting, the file it lives in, its value
  and how to change it.
- [The data model](docs/DATA-MODEL.md) — the records, and the rules that help to maintain their
  integrity.
- [The method](docs/METHOD.md) — estimate first, gate, measure, compare.
- [A sample of findings to date](writing/what-the-register-measured.md)

## Tests

Python 3.9 or later, standard library only, no network access. From the top of the repository:

```
python -m unittest discover -s store/tests
python -m unittest discover -s wrapper/tests
python -m unittest discover -s estimator/tests
python -m unittest discover -s ledger/tests
```

GitHub Actions runs the same suites on every push.

These are the tests the authors used while building Reckon. They are by no means exhaustive, and
your own use may need more; your mileage may vary.

## Licence

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
