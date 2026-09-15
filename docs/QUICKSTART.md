# Quickstart

## Install

Before you start, in Terminal run `python3 --version`. On macOS, `python3` and `git` come with
Apple's Command Line Tools, not with macOS itself. The Tools are a separate, much smaller download
than the Xcode app, and Reckon does not need Xcode. If they are not installed yet, macOS offers to
install them the first time you type either command. Which Python you get depends on the version of
the Tools rather than the version of macOS. Reckon needs Python 3.9 or later. It was tested on
macOS 26.4 with Command Line Tools 26.5, whose `python3` is Python 3.9.6 with pip 21.2.4. If yours
is older than 3.9, update the Command Line Tools through Software Update, or install Python from
python.org.

Then, on macOS, in Terminal:

```sh
git clone https://github.com/hyperhodge/reckon.git
cd reckon
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

That downloads Reckon into a folder called `reckon`, makes a Python environment for it inside that
folder, and installs the `reckon` command into the environment. Each line matters on macOS:

- macOS has `python3` and `pip3`, not `python` or `pip`, until the environment is active. Inside it,
  both work.
- The pip that comes with the Command Line Tools' Python 3.9 is too old to install a folder like this
  one, so it is upgraded first.
- In a new Terminal window, run `source .venv/bin/activate` from the `reckon` folder before using `reckon`.

`pip install -e .` is the only supported install. Reckon keeps its data beside its code, so the code
that runs has to be the folder you downloaded. Run `reckon` any other way -- copied out of its
folder, installed from a wheel, or without `-e` -- and it refuses, naming the line to use (see
[SETTINGS](SETTINGS.md)).

Every command below ran against this repository's own `sample/`. Your numbers will differ once you
run these against your own proposals, task files and transcripts; the shape of the output will not.
Where a capture shows `<...>`, that part differs from one computer to the next.

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

The command exits with 0 for allow, 2 for ask and 3 for block, so a script can tell the three
apart.

With Claude Code installed, the gate shows the exact command it would run, on a `command` line.
`<claude>` is where Claude Code is installed on your computer, and `<session id>` is new on every
run:

```
$ reckon gate sample/tasks/P-0001-T08.md
task     P-0001-T08  sample/tasks/P-0001-T08.md
state    approved
estimate $14.00  (source: front_matter) -- est_p95_usd from the task file's front matter
gate     ASK against $10.00 in gate.json
         - estimated p95 $14.00 is above the $10.00 threshold in gate.json (est_p95_usd from the task file's front matter).
rail     subscription
command  <claude> -p "$(cat sample/tasks/P-0001-T08.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id <session id>

ASK. Nothing was run. Answer the points above, then re-run with --execute once they are settled.
```

Without Claude Code, the `command` line becomes a `shape` line and three notes saying the command
was not checked. Everything else is the same. The next two examples show that form:

```
$ reckon gate sample/tasks/P-0001-T08.md
task     P-0001-T08  sample/tasks/P-0001-T08.md
state    approved
estimate $14.00  (source: front_matter) -- est_p95_usd from the task file's front matter
gate     ASK against $10.00 in gate.json
         - estimated p95 $14.00 is above the $10.00 threshold in gate.json (est_p95_usd from the task file's front matter).
rail     subscription
shape    claude -p "$(cat sample/tasks/P-0001-T08.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id <session id>
         UNVERIFIED. No `claude` executable on PATH or in <the usual install folders>.
         This is the shape the launcher would build, not a command it has checked.
         Set CLAUDE_CLI to its path if it is installed elsewhere.

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
shape    claude -p "$(cat sample/tasks/P-0001-T09.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id <session id>
         UNVERIFIED. No `claude` executable on PATH or in <the usual install folders>.
         This is the shape the launcher would build, not a command it has checked.
         Set CLAUDE_CLI to its path if it is installed elsewhere.

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
shape    claude -p "$(cat sample/tasks/P-0001-T10.md)" --allowedTools Read,Edit,Write,Bash --permission-mode acceptEdits --output-format json --session-id <session id>
         UNVERIFIED. No `claude` executable on PATH or in <the usual install folders>.
         This is the shape the launcher would build, not a command it has checked.
         Set CLAUDE_CLI to its path if it is installed elsewhere.

ASK. Nothing was run. Answer the points above, then re-run with --execute once they are settled.
```

## `reckon record`

Turns Claude Code transcripts into Run records, then adds them to the store. Run here against
`sample/session/`, which ships one invented session -- not a real transcript -- so there is
something to record on a fresh install.

`reckon record` writes into the folder you downloaded: `ledger/runs.json`, `runs/` and
`store/refs.json`, because Reckon keeps its data beside its code. Those are listed in `.gitignore`,
with `.venv/` and the install's own files, so `git status` stays empty.

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

-> <your reckon folder>/ledger/runs.json

new        R-0001  10203040  $0.01
unchanged  0
-> appended 1 to <your reckon folder>/runs/2026-09.jsonl
```

The first note is expected here: the invented session names a file it wrote, and that file was not
on this computer, so the record keeps the file's name and leaves its size and fingerprint empty.

## `reckon report`

A read-only summary of what ran, including any task whose recorded cost is more than 10% over its
estimate. Prints only; nothing is written. `--sample` reads the sample shipped with the package
instead of your own store, which is what a fresh install has to show.

```
$ reckon report --sample
reckon report -- the sample shipped with the package (register-sample.json)
Every figure here is a client-side estimate at list rates, from the task's own record and its sessions' own transcripts. The estimate is a range, not a point; the recorded cost is a floor on what a task drew, not a bill for it.

Tasks more than 10% over their estimate:
  P-0001-T10     estimate $14.00   recorded $30.13   (+115%)
```

## Your own work

**Record your own sessions.** With no `--source`, `reckon record` reads the transcripts Claude Code
keeps on your computer, in `~/.claude/projects`:

```sh
reckon record
```

It reads every session still in that folder, including sessions from before you installed Reckon.
Two kinds of work it cannot see: sessions Claude Code has already deleted, which by default is any
older than 30 days (Claude Code's `cleanupPeriodDays` setting), and chats in the Claude apps or on
claude.ai, which are not Claude Code sessions and leave no transcript on your computer. Nothing is
recorded automatically: run `reckon record` again after new sessions. A second run adds only the
sessions it has not seen, shows a session that has grown since as `corrected`, and counts the rest
as `unchanged`.

**See what was recorded.** Without `--sample`, `reckon report` reads your own store. A new store
has runs but no tasks, and says so:

```
$ reckon report
reckon report -- the reader's own store
Every figure here is a client-side estimate at list rates, from the task's own record and its sessions' own transcripts. The estimate is a range, not a point; the recorded cost is a floor on what a task drew, not a bill for it.

No tasks in this store yet, so there is no estimate to compare a recorded cost with.
Runs recorded: <how many>, $<their total> at list rates.
```

The list of tasks over their estimate needs task records linked to runs, which this version does
not create for you; the sample above shows what that list looks like.

**Gate a task of your own.** Copy a sample task into a folder of your own. `my-tasks/` is listed in
`.gitignore`, so your files do not show up as changes to the download:

```sh
mkdir -p my-tasks
cp sample/tasks/P-0001-T09.md my-tasks/my-task.md
```

Open `my-tasks/my-task.md` in any text editor and change these fields at the top:

- `ref:` a label of your own, such as `MY-TASK-1`.
- `brief:` one line saying what the task is for.
- `est_p95_usd:` your estimate in US dollars -- for example the `high` figure `reckon estimate`
  gives for the same work.

Leave `state: approved` as it is: the gate blocks a task in any other state. Replace the text under
the second `---` with what you want Claude Code to do; that text is the prompt `--execute` sends.
Then:

```sh
reckon gate my-tasks/my-task.md
```

It answers allow, ask or block, as in the examples above. Nothing runs until you add `--execute`.

## Read these

- [Getting started and settings](SETTINGS.md) — every setting, the file it lives in, its value
  and how to change it.
- [The data model](DATA-MODEL.md) — the records, and the rules that help to maintain their
  integrity.
- [The method](METHOD.md) — estimate, gate, measure, compare.
