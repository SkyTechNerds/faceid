# Measuring instead of guessing

Every number in this project came from one of these reports rather than from intuition.
Run them on your own instance before changing a setting — the answer is usually not the
one that feels obvious.

← back to the [README](../README.md)

## Without a terminal: the Tools tab

Switch it on under **Settings → *Does it actually work?* → show the Tools tab**. It runs the same
analyses in the service itself and shows them as tables:

| Tool | Answers | Cost |
|---|---|---|
| How fast is a recognition? | delay from the start of the event to the published name, per attempt and camera | instant, reads the history |
| How well is each person covered? | angles, cameras, day and night per person, and what photo is concretely missing | re-reads every reference photo |
| Why do events yield no face? | no face at all / too small / detector unsure, per camera | downloads one snapshot per event |

This is not a wrapper around the scripts below, because it could not be: `scripts/` is not
part of the app image at all, and the delay measurement there reads `journalctl`, which does
not exist in a container without systemd. If you run FaceID as a Home Assistant app, the tab
is the only way to get these numbers.

The delay figures therefore come from the history rather than the log, which also means they
need no camera access. Recognitions produced by a history scan are excluded instead of being
averaged in — they lag their event by weeks — and the tab says how many it dropped.

## With a terminal: the scripts

Three scripts answer the questions that otherwise invite guesswork:

```bash
python scripts/why-no-face.py --days 7 --clip 12   # why do events yield no face?
python scripts/coverage.py                         # what is each person missing?
python scripts/measure-recognition.py --baseline /tmp/old --days 3
```

Start with `why-no-face.py` if recognition feels rare. It counts *why* events are
discarded — no face at all, too small, no snapshot — and separates the hopeless cases
(person too far away) from the recoverable ones (the snapshot moment was bad). With
`--clip` it re-checks discarded events against the recording, which tells you what
`clip_fallback` is worth **on your cameras** rather than on mine. Most "it barely
recognises anyone" reports turn out not to be gallery problems at all.

`coverage.py` reports, per person: photo count, diversity, viewing angles from the
landmarks, which cameras she was enrolled from, greyscale/IR shots, and a leave-one-out
self test — then names the concrete gap.

`measure-recognition.py` compares the current gallery against an older one (unpack a
backup from `data/backups`) and adds a practical probe against recent Frigate events,
including how much headroom each recognition has above the threshold. Events whose face
is already in the gallery are excluded — they score ~1.0 and measure nothing.

**Running FaceID as a Home Assistant app?** You have no shell, so the part that decides
your threshold was moved into the UI: Settings → *Does it actually work?* runs the
leave-one-out test and the practical probe as a background job and reports the one number
that matters, how high a stranger got. That covers threshold and `match_top_k`.

Two things still need a shell, and neither is required to run FaceID well:

* the **coverage report** (`coverage.py`) — which angles, cameras and IR shots each
  person is missing
* **comparing two galleries** (`--baseline`) — useful after a round of enrolling, but the
  UI analysis already tells you where you stand today
