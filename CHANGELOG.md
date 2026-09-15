# Changelog

## 1.1.0

- **One command, `reckon`,** over `estimate`, `gate`, `record` and `report` -- each subcommand
  a thin wrapper over the module that already did the work; no calculation was copied.
- **The only supported install is `pip install -e .`** from a downloaded copy; run any other way,
  `reckon` refuses and names the line to use.
- **`docs/QUICKSTART.md`** -- install, then every command, with its own captured output.
- **`reckon report`** lists any task whose recorded cost is more than 10% over its estimate, on the
  face saying these are list-price estimates and a floor, not a bill.
- **The sample's gate table is the real gate's own answer,** run on copies of three sample tasks
  with their state changed to `approved` -- the tasks themselves are shipped, and the same gate
  blocks a shipped task on sight. `sample/tasks/` and `sample/session/` ship those copies and one
  invented Claude Code session, each marked as such in its first line.
- Wording fixes so the estimator's and the walkthrough's own text pass this package's language
  check.

## 1.0.0

- First published snapshot: the data model, the cost gate, the estimator, and the ledger reader,
  with a redacted sample and the getting-started, data-model and method documents.
