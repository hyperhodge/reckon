# Getting started and settings

Reckon ships with the values it was used with, so it works as it is. Every setting is a value in a
plain JSON file beside the code that reads it. To change one, edit the value in that file; the next
run uses it. Nothing has to be set up before the first run.

## Getting started

1. **Check Python.** Python 3.9 or later. Reckon uses the standard library only.
2. **Run the tests** (below). They need nothing else: no network, no Claude Code, no records of your
   own.
3. **Read [the method](METHOD.md)**, then set the gate threshold and the session ceiling below to
   numbers that suit your own work.
4. **Measure real sessions.** `ledger/aggregate.py` and `ledger/recorder.py` read the transcripts
   Claude Code keeps on your machine. Keep a copy of those transcripts elsewhere if you want a long
   history: Claude Code prunes its own folder.

## Settings

| Setting | File | Value now | What it does | How to change it |
|---|---|---|---|---|
| `threshold_usd` | `wrapper/gate.json` | `10.0` | The gate asks a person before a task whose estimate, at the 95th percentile, is above this many US dollars. | Edit the value. |
| `estimate_scope` | `wrapper/gate.json` | `"brief"` | What the gate prices: the task's written brief. | Edit the value. |
| `min_samples_for_prior` | `wrapper/gate.json` | `3` | With fewer past runs than this for a class of task, the gate asks rather than guessing. | Edit the value. |
| `api_rail_cap_ceiling_usd` | `wrapper/gate.json` | `5.0` | The largest cap a person may set when opting one task onto a pay-as-you-go account. | Edit the value. |
| `priors` | `wrapper/gate.json` | Past costs per class of task | The history the gate prices a brief against. | Derived: run `python ledger/recorder.py --priors --write-priors`. Do not edit by hand. |
| `defaults` | `wrapper/gate.json` | `acceptEdits`; tools `Read`, `Edit`, `Write`, `Bash`; output `json` | The permission mode, tools and output format the launcher passes to Claude Code. | Edit the values. |
| `session_ceiling_gbp` | `wrapper/gate.json` | `10.0` | The ceiling on one conversational session, in pounds. | Edit the value. |
| `session_ceiling_assistant_messages` | `wrapper/gate.json` | `90` | The same ceiling in assistant messages. Cost per message rose steeply above about 110. | Edit the value. |
| `session_ceiling_action` | `wrapper/gate.json` | `"no-new-scope, finish-in-hand, hand off"` | What a session does at its ceiling. | Edit the value. |
| `models` | `ledger/prices.json` | Published list prices per model | US dollars per million tokens, by kind of token. | Edit a price, and update `checked_on`. |
| `default`, `rules` | `ledger/rails.json` | `"subscription"`, no rules | Which billing rail a session ran on. | Add a rule with a date range and a reason when your billing changes. |
| `age_days` | `store/purge.json` | `90` | How long a file goes unused before it is queued for purging. Purge queues files and records consent; it does not delete. | Edit the value. |
| `exempt_paths` | `store/purge.json` | `projects`, `tasks`, `runs`, `risk`, `decisions`, `store/refs.json` | Paths that are not queued for purging. | Edit the list. |
| `read_only_roots` | `store/purge.json` | `~/.claude/projects` | Folders Reckon reads and does not write to. | Edit the list. |
| `candidate_roots` | `store/purge.json` | A transcript archive folder, and working files named by Run records | Where purge looks for candidates. | Edit the paths. |
| The fitted model | `estimator/model.json` | Fitted to the history this package was built from | What the estimator predicts from. | Derived: run `python estimator/estimate.py --fit`. Do not edit by hand. |
| `LIST_PATH` | `store/names_check.py` | A private file in your user folder, outside the repository | The list of client and personal names the names check looks for. The check refuses a list that other users can read. | Edit the path in the code, or pass `--list`. |

The session ceiling is a rule a session follows. The gate stands in front of scripted tasks only; it
does not stop a conversation.

## Running the tests

From the top of the repository:

```
python -m unittest discover -s store/tests
python -m unittest discover -s wrapper/tests
python -m unittest discover -s estimator/tests
python -m unittest discover -s ledger/tests
```

The tests need no records of your own: each one builds the data it checks in a temporary folder, or
reads the settings files shipped with the package.

These are the tests the authors used while building Reckon. They are by no means exhaustive: they
cover what the authors needed, and your own use may need more.
