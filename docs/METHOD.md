# The method

**Estimate before the work starts, gate it, measure what it cost, and compare the two.**
Nothing here is clever. The value is that every step leaves a record, so the estimates can be
checked against reality instead of remembered.

## 1. Estimate first

Before a piece of work starts, the session states its estimate out loud — a cost range and a
number of assistant messages — and the estimate is logged. **Estimate turns and context growth, not
output tokens.** In the history behind this package, output was about a seventh of the cost; the
rest was the conversation being re-read on every turn, so cost grows with the number of turns
multiplied by the size of the context.

## 2. The gate

`wrapper/gate.py` prices a task from its brief before it runs, against a threshold held as data in
`wrapper/gate.json`, and answers **allow**, **ask** or **block**. Above the threshold it asks a
person. A task with no usable cost history for its class asks rather than guessing. A run that
would bill a pay-as-you-go account instead of a subscription is blocked unless a person opted in on
that task, with a cap, and even then it asks.

**The gate prices the brief, not the session.** It sees the task as written, before any work
starts. If more work is asked for once the session is under way, that work was not in the brief the
gate priced. So a session can cost far more than the gate expected, even though the gate's estimate
was reasonable for what it was shown.

## 3. A ceiling on each session

A session has a ceiling in money and in assistant messages. At the ceiling it **stops taking new
work, and does not stop working**: it finishes the item in hand, records what happened, and writes
the starting text for the next session. Cost per message was flat below roughly 110 messages and
rose steeply above it, which is why the message ceiling sits below that point.

The ceiling is set in `wrapper/gate.json`: `session_ceiling_gbp` is `10.0` and
`session_ceiling_assistant_messages` is `90`. Every setting, its value and how to change it are in
[Getting started and settings](SETTINGS.md).

## 4. Measure

`ledger/recorder.py` reads a finished session's transcript and writes one Run record: tokens by
kind, the model, the rail, and a cost at list prices from `ledger/prices.json`. **It is an estimate
at list rates, not a bill**, and a run read before its transcript is complete is a floor that a
later record supersedes. The estimate is also a floor on what an account uses: usage outside the
coding assistant bills the same account and leaves no transcript to read.

## 5. Compare

Every estimate is logged beside its measured actual. Before any of that, `estimator/estimate.py`
breaks the proposal into pieces: a mechanical split, not a reading of the work. It splits on a new
line, a semicolon or bullet, a dash, a comma followed by "and", or the words "then" / "and then",
and drops fragments of four characters or less as punctuation noise; a proposal with no such break
is priced as one piece. Where the wording states an explicit count — "the 16 acceptance queries",
"four items" — that number is read directly as a size signal, capped at 16, because a stated count
has been the single most reliable one in the log. Each piece is priced and printed on its own line
(`reckon estimate` shows "BROKEN INTO N PIECE(S)"), so a split the rule got wrong is visible rather
than hidden inside a total.

`estimator/estimate.py` learns from that log:

- **Turns from the proposal.** A ridge regression on the logarithm of turns, over features of the
  proposal's words — how many items it names, how long it is, whether it is a build or an
  investigation.
- **Cost from turns.** A curve fitted to Run records: cost grows with messages, and faster than
  linearly.
- **A realisation factor.** Measured at about 2.3 times: a brief grows on its way to the session
  that does it, and that session's cost grows again on its way to a shipped result.

## 6. How good were the estimates?

The estimator was tested on 27 past pieces of work. Each one was predicted by a model that had not
seen it, and each prediction was a range the real number of messages should fall inside 80% of the
time.

- **The real figure fell inside the range 21 times out of 27, or 78%**, close to the 80% it aimed
  for.
- **Estimates written by hand in the same log were right 53% of the time.**
- **The wording of a proposal says little about its size.** Counting the items a proposal names
  does only a few per cent better than ignoring the proposal altogether.
- **The range for a single piece of work is wide**: its top is about six times its bottom. One
  estimate tells you roughly what kind of job something is, not what it will cost. Added up over
  many pieces of work, the ranges give a total that is useful for planning.

The estimator's result on the sample's own tasks is in `sample/SAMPLE.md`.
