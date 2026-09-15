# Quickstart

Download the repository, then, from its top level:

```
$ pip install -e .
```

That is the only supported install. Run `reckon` any other way -- copied out of its folder,
installed from a wheel, or without `-e` -- and it refuses, naming the line above (decision 1;
see [SETTINGS](SETTINGS.md)).

Every command below ran against this repository's own `sample/`. Your numbers will differ once
you run these against your own proposals, task files and transcripts; the shape of the output
will not. The `claude` binary path on the `command` line under `reckon gate` is this machine's
own install and will differ on yours.

## `reckon estimate`

Prices a proposal before it runs -- broken into pieces, and given as a range rather than a point.

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

## `reckon gate`

Decides whether a task file may run -- allow, ask or block -- against its own estimate and the
threshold in `gate.json`. Prints only; add `--execute` to actually run it. A task that is not
`approved` is blocked outright, so the three examples below run on copies of the sample tasks at
`sample/tasks/`, each with its state changed to `approved` -- the tasks themselves shipped, and
the gate blocks a shipped task on sight.

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

```
$ reckon gate sample/tasks/P-0001-T09.md
task     P-0001-T09  sample/tasks/P-0001-T09.md
state    approved
estimate $4.00  (source: front_matter) -- est_p95_usd from the task file's front matter
gate     ALLOW against $10.00 in gate.json
         - estimated p95 $4.00 is at or under the $10.00 threshold (est_p95_usd from the task file's front matter).
rail     subscription
command  /tmp/reckon-bin/claude -p "$(cat sample/tasks/P-0001-T09.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id 5242764e-7c44-4499-9ae0-bda0722cb7e5

Dry run. Nothing was run. Add --execute to spend tokens on it.
```

```
$ reckon gate sample/tasks/P-0001-T10.md
task     P-0001-T10  sample/tasks/P-0001-T10.md
state    approved
estimate $14.00  (source: front_matter) -- est_p95_usd from the task file's front matter
gate     ASK against $10.00 in gate.json
         - estimated p95 $14.00 is above the $10.00 threshold in gate.json (est_p95_usd from the task file's front matter).
rail     subscription
command  /tmp/reckon-bin/claude -p "$(cat sample/tasks/P-0001-T10.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id 618a45e5-7ad7-4a95-bcc5-9ecfe5890f3e

ASK. Nothing was run. Answer the points above, then re-run with --execute once they are settled.
```

## `reckon record`

Turns Claude Code transcripts into Run records, then intakes them into the store. Run here
against `sample/session/`, which ships one invented session (decision 2 -- not a real
transcript) so there is something to record on a fresh install.

```
$ reckon record --source sample/session
Source: explicit --source — sample/session
1 run(s), 1 output(s), $0.01 total at list rates (client-side estimate, not a bill).

run         started           project                  cost $  out  task
------------------------------------------------------------------------------
R-10203040  2026-09-01 10:00  reckon-sample              0.01    1  —

NOTE: 1 recorded output(s) no longer exist on disk. They are kept as records with sha256 and bytes null, not dropped.
NOTE: 1 of 1 runs have no task ref. task_ref is null rather than guessed; add the link in ledger/run-tasks.json.
NOTE: 1 of 1 runs take the default rail with no matching rule in rails.json.

-> /private/tmp/reckon-trial-02/ledger/runs.json

new        R-0001  10203040  $0.01
unchanged  0
-> appended 1 to /private/tmp/reckon-trial-02/runs/2026-09.jsonl
```

## `reckon report`

A read-only summary of what ran, including any task whose recorded cost is more than 10% over
its estimate -- decision 7. Prints only; nothing is written. `--sample` reads the sample shipped
with the package instead of your own store, which is what a fresh install has to show.

```
$ reckon report --sample
reckon report -- the sample shipped with the package (register-sample.json)
Every figure here is a client-side estimate at list rates, from the task's own record and its sessions' own transcripts. The estimate is a range, not a point; the recorded cost is a floor on what a task drew, not a bill for it.

Tasks more than 10% over their estimate:
  P-0001-T10     estimate $14.00   recorded $30.13   (+115%)
```

## Read these

- [Getting started and settings](SETTINGS.md) — every setting, the file it lives in, its value
  and how to change it.
- [The data model](DATA-MODEL.md) — the records, and the rules that help to maintain their
  integrity.
- [The method](METHOD.md) — estimate, gate, measure, compare.
