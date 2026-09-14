# Three sceptical questions I asked AI this week, and what each one changed

*A build log from one week of running Claude Code sessions on my own project. Every figure is a
client-side estimate at list rates, read from my own transcripts. It is a floor on what the account
used, not a bill.*

---

## Question one: is that confident answer actually true?

On day one I asked the assistant why a cost-estimating routine I was using focused almost entirely
on output tokens. The answer came back sure of itself. Output costs roughly five times as much as
input per token, so output is where the money goes.

It sounded right. I didn't take it on trust. I had a small tool built that reads my session
transcripts and prices every message at published rates, then ran it over several weeks of history.

| Share of estimated spend | |
|---|---|
| Output | 17% |
| Everything on the input side | 83% |
| Cache reads alone | 57% |

Cache reads cost 3.39 times what output did. If you run coding agents seriously you already know
that context is the bill, so none of this is news. What mattered to me was that I'd been handed the
opposite with total confidence, and checking it took days rather than becoming an assumption I
carried into everything after.

It changed how I work straight away. Shorter answers can save at most 17%. Short sessions, lean
standing instructions, and starting fresh instead of resuming a long session go after the other 83%.

## Question two: is the list actually getting shorter?

A few days in, it didn't feel like it. So I had the tool count.

| Day | Opened | Shipped | Net |
|---|---|---|---|
| 5 September | 6 | 0 | +6 |
| 6 September | 10 | 0 | +10 |
| 7 September | 21 | 14 | +7 |
| 10 September | 12 | 7 | +5 |
| 11 September | 8 | 0 | +8 |

It did not shrink on any day. Fifty-eight items, thirty-five still open.

There may well have been too much work; I can't measure that. What I can say is that I gave
requirements on day one and the AI ignored some of them until I raised them again. My brief held
thirty-six distinct requirements. Six days later, nineteen of them still had no item on the list,
and one I'd given on day one was written up as brand new when I mentioned it again. The AI had not
broken the task down into its parts.

A competent team doesn't start building from a conversation. It writes the product requirements and
the technical design first: what's in scope and what isn't, every functional requirement with an
ID, the non-functional ones like security and performance, the dependencies and the milestones. The
list gets long on day one and shorter every day after. The AI skipped that step and went straight to
building, so every day turned up work a proper breakdown would have shown at the start.

"Opened" is exact. "Shipped" is approximate, taken from dates in each finished item's notes. No
reasonable reading of it produces a day that got shorter, and it counts items, not their value.

## Question three: is this number useful, or even accurate?

The point of the whole exercise was to price work before paying for it. So the next thing built was
an estimator, tested against past sessions. It reported a 78% hit rate against a target of 80%,
which sounded excellent.

I asked how wide the ranges were. Wide enough to be close to worthless: the top of a typical range
was about ten times the bottom in money. A range of £0 to £200 would score 100% on the same test,
and be totally useless as an estimator. It also gave an identical price for "add a login screen" and
"rebuild the whole reporting system with exports and a new database". Same root cause: every item
was a one-line title, so there was nothing in it to size. Giving the estimator a breakdown of each
piece of work to size, instead of a title, is logged as future work and is not in this snapshot.

A hit rate quoted without the width of the range is a forecast of rain some time this month. Both
numbers are now reported together, and the width is on record as the reason the estimator isn't
ready to trust.

## Then I paused

By then the reading was clear. The measurement had earned its keep, and most of what was left in the
plan was tooling for its own sake. I set one last piece of work, then this write-up, and paused, to
take time for other projects and to think.

Using a subscription for part of this reduced my costs significantly, but the time it took to
supervise the AI build gave a much higher opportunity cost overall.

## What I'd pass on

The AI did the building well. What it didn't do was think the work through before starting, or check
its own answers. Those were my three questions:

- **Is that confident answer true?**
- **Has the work been broken down, or just started?**
- **Is this number useful, or even accurate?**

Each took a minute to ask, and each one changed what happened next.

---

*Measured with a tool built with Claude Code to read my own session transcripts and price them at
published rates. Figures are estimates, not invoices.*
