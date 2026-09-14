# The data model

Everything is a plain file. No database, no server, no third-party packages. The code is in
`store/store.py`.

## The records

| Record | Where it lives | Shape | Changes by |
|---|---|---|---|
| **Project** | `projects/P-NNNN.md` | Markdown with front matter | Editing, through stage transitions |
| **Task** | `tasks/P-NNNN-TNN.md` | Markdown with front matter | Editing, through task transitions |
| **Run** | `runs/YYYY-MM.jsonl` | One JSON line per session | **Appending only** |
| **Decision** | `decisions/YYYY-MM.jsonl` | One JSON line per decision | **Appending only** |
| **Reference register** | `store/refs.json` | JSON | Allocation only |

**A Project** is a piece of work worth tracking: its one-liner, its stage (for example
`prototype`), what data it touches, whether it is an AI system and in what role, and where its
files live.

**A Task** belongs to a Project: its brief, its state (`drafted`, `approved`, `running`, `built`,
`shipped`), who approved it and when, its cost estimate at the 95th percentile, and the Runs that
did it.

**A Run** is one assistant session: when it started and ended, the model, the billing rail, token
usage by kind, the estimated cost at list rates and how that cost was derived, and the Task it
served.

**A Decision** is anything a person decided that Reckon must be able to show later: a task moving
state, a gate overridden, a session started or stood down, an export consented to.

## The rules that help to maintain integrity

1. **A reference is allocated once and not reused.** `P-0001`, `P-0001-T08` and `R-0018` are
   written into the reference register the moment they exist, bound to a natural key. Nothing
   recomputes them.
2. **Corrections are new records, not edits.** Runs and Decisions are append-only. A Run read
   before its transcript was complete is superseded by a later record, and both are kept.
3. **The append-only folders are checked.** `store/integrity.py` holds each file's length and a
   hash of those bytes, and reports a file that was edited, truncated or half-written. It reports
   and does not repair.
4. **Stage gates refuse.** Moving a record forward into a stage it owes fields for is refused, and
   the refusal names every missing field. An override needs a name and a reason, and is itself a
   Decision.
5. **A value that could identify anyone lives in a named field**, so presentation mode can redact
   it. `store/governance.py` redacts every disclosive field, replaces every person with
   `[Approver]`, and audits its own output: an export where anything survives is refused.
6. **Nothing is shared until the names check passes.** `store/names_check.py` looks for every form
   of every name on a private list, kept outside the repository, and plants every name in test text
   first, so a check that has stopped matching cannot pass silently.

## The sample

`sample/register-sample.json` holds one Project, three Tasks, their Runs and the Decisions about
them, exactly as presentation mode redacts them. Free text reads `[P-0001-T08 · brief]` and similar;
the structure, states, dates, token counts and costs are real.
