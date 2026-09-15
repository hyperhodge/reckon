# SPDX-License-Identifier: Apache-2.0
"""reckon report: a read-only summary of the store, or of the sample shipped
with the package. Decision 7, RECKON-1.1-SPEC.md: lists any task whose
sessions cost more than 10% over its estimate, with the estimate-not-a-bill
wording on its face. Prints only; writes nothing, so no consent is needed
(store.py's export consent gate is about writing an export, not this).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SAMPLE_PATH = ROOT / "sample" / "register-sample.json"

NOT_A_BILL = (
    "Every figure here is a client-side estimate at list rates, from the task's own "
    "record and its sessions' own transcripts. The estimate is a range, not a point; "
    "the recorded cost is a floor on what a task drew, not a bill for it."
)

OVER_THRESHOLD = 0.10


def _store():
    import importlib
    sys.path.insert(0, str(ROOT / "store"))
    return importlib.import_module("store")


def _load_live(root):
    store = _store()
    data = store.load_store(root)
    runs = list(store.current_runs(data["runs"]).values())
    return data["tasks"], runs


def _load_sample(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload.get("tasks", {}), payload.get("runs", [])


def cost_by_task(runs):
    totals = {}
    for run in runs:
        ref = run.get("task_ref")
        if not ref:
            continue
        totals[ref] = totals.get(ref, 0.0) + (run.get("cost_usd") or 0.0)
    return totals


def _as_float(value):
    """Front matter is flat text, so a real store's est_p95_usd arrives as a
    string ('14.0'), never a number -- unlike this module's own tests, which
    is exactly how the walkthrough (RECKON-1.1-SPEC.md R3) caught this."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def overruns(tasks, totals, threshold=OVER_THRESHOLD):
    """Tasks whose recorded cost is more than `threshold` over est_p95_usd.
    Decision 7's comparison; the whole point is this list, not the totals."""
    rows = []
    for ref, task in tasks.items():
        fields = task.get("fields", task)
        estimate = _as_float(fields.get("est_p95_usd"))
        spent = totals.get(ref)
        if not estimate or spent is None:
            continue
        ratio = (spent - estimate) / estimate
        if ratio > threshold:
            rows.append({"ref": ref, "estimate_usd": estimate, "spent_usd": spent,
                        "over_pct": ratio * 100})
    rows.sort(key=lambda r: -r["over_pct"])
    return rows


def run(sample=False, root=None, out=None):
    out = out or sys.stdout
    root = Path(root) if root else ROOT

    if sample:
        tasks, runs = _load_sample(SAMPLE_PATH)
        source_label = f"the sample shipped with the package ({SAMPLE_PATH.name})"
    else:
        tasks, runs = _load_live(root)
        source_label = "the reader's own store"

    print(f"reckon report -- {source_label}", file=out)
    print(NOT_A_BILL, file=out)
    print(file=out)

    rows = overruns(tasks, cost_by_task(runs))
    if not rows:
        print(f"No task's recorded cost is more than {OVER_THRESHOLD*100:.0f}% "
              f"over its estimate.", file=out)
        return 0

    print(f"Tasks more than {OVER_THRESHOLD*100:.0f}% over their estimate:", file=out)
    for row in rows:
        print(f"  {row['ref']:<14} estimate ${row['estimate_usd']:.2f}   "
              f"recorded ${row['spent_usd']:.2f}   (+{row['over_pct']:.0f}%)", file=out)
    return 0
