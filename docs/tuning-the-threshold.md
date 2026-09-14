# Calibrating the threshold

What `match_threshold` actually trades away, how to find your own number, and why the
people you enrolled — not strangers — are usually the limit.

← back to the [README](../README.md)

The defaults are deliberately cautious, and on a real gallery that caution turned out to
be expensive. Measure yours rather than guessing — the Tools tab produces the numbers
without a terminal, as do `scripts/measure-recognition.py` and `scripts/coverage.py`.

On a 128-photo household gallery the separation was far wider than the defaults assume:

| | correct person | someone else in the gallery |
|---|---|---|
| best match score | median 0.50 | median 0.18, **max 0.31** |

With a default threshold of 0.50 sitting exactly on the median of correct matches, half
of all genuine recognitions were discarded to keep a distance nothing ever came close to.
Two changes followed, both measured:

* **`match_top_k` 3 → 1.** Averaging the best k photos punishes people whose references
  cover many angles — their own less similar photos drag the score down, so the
  best-covered people scored worst.
* **`match_threshold` 0.50 → 0.45**, still 0.14 above the highest score any stranger
  ever reached.

Together these lifted recognition on a held-out set of real events from 90% to 100%, and
moved the weakest favourite's worst match from 0.01 above the cut-off to 0.09 above it —
without a single misassignment.

**In the UI:** Settings → *Does it actually work?* runs the same analysis as a background
job — no shell needed, which matters if you run FaceID as a Home Assistant app. The
scripts remain for scripted or comparative runs (`--baseline`, `--top-k`).

## Strangers are rarely the limit — the people you enrolled are

The obvious question is "how close does a stranger get?", and it is the wrong one to stop
at. Two numbers matter, and the **higher** of them sets your floor:

| | measured here |
|---|---|
| highest score a **stranger** reaches | 0.195 |
| highest score between two **enrolled** people | **0.411** |

Going by strangers alone, 0.25 would look safe. It is not: at 0.25 the two people in this
household who resemble each other most become interchangeable — a father and daughter whose
galleries overlap at 0.411. And a *wrong known name* is worse than no name at all, because
automations act on it, while "unknown" can simply be ignored.

This is why the analysis reports both, and why lowering the threshold because someone is
not being recognised is usually the wrong move. (One case where no threshold helps: **small
children** — a toddler's face is about half the size of an adult's and falls below
`min_face_px` at the same distance. See
[the pipeline doc](recognition-pipeline.md#small-children-are-a-hard-case-not-a-tuning-problem).) If a person sits just below the line, the
fix is more reference photos of the situations they are actually seen in — that raises
*their* score without moving anyone else closer.

Do not copy any of these numbers. Measure your own gallery: Settings → *Does it actually
work?* reports the ceiling for both cases, and refuses to suggest lowering anything if it
finds a misassignment.
