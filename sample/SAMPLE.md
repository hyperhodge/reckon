# The sample, in words

1 project(s), 3 task(s), 4 run(s) and 3 decision(s), redacted: every field naming a person reads [Approver], and free text is replaced.

## The runs

| Run | Task | Model | Started | Cost at list rates, USD |
|---|---|---|---|---|
| R-0018 | P-0001-T08 | claude-opus-5 | 2026-09-06T08:17:44.982Z | 12.55 |
| R-0019 | P-0001-T09 | claude-opus-5 | 2026-09-06T08:50:12.024Z | 4.37 |
| R-0022 | P-0001-T10 | claude-opus-5 | 2026-09-06T19:35:58.358Z | 15.66 |
| R-0023 | P-0001-T10 | claude-opus-5 | 2026-09-06T20:04:38.489Z | 14.47 |

## The estimator on the sample

Leave-one-out: each task predicted by a model fitted without it.

| Task | Actual turns | Predicted range | Inside the range |
|---|---|---|---|
| P-0001-T08 | 74 | 19 to 102 | yes |
| P-0001-T09 | 16 | 17 to 92 | no |
| P-0001-T10 | 112 | 16 to 85 | no |

## The gate on the sample

The gate asks a person when a task's estimate is above its threshold, $10.00. Run for real (`wrapper.gate.decide`) on copies of these tasks marked approved -- as shipped, every one of them is `shipped`, not `approved`, and the same gate blocks all three on that alone.

| Task | Estimate, USD at the 95th percentile | Threshold, USD | The gate |
|---|---|---|---|
| P-0001-T08 | 14.00 | 10.00 | ask |
| P-0001-T09 | 4.00 | 10.00 | allow |
| P-0001-T10 | 14.00 | 10.00 | ask |
