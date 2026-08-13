# How a face actually gets recognised

FaceID tries up to three sources for every Frigate event. They form a **cascade**: each
stage runs only when the one before it came up empty, so a normal recognition costs
nothing extra.

```
Frigate event
      │
  ① SNAPSHOT ──────────────── face? ──► match, done          ~0.3s
      │ no                                                    (up to max_attempts, 2.5s apart)
      ▼
  ② LIVE HI-RES FRAME ─────── face? ──► match everyone in it  ~1s
      │ no                                                     once per event, opt-in
      ▼
  ③ RECORDING SCAN ────────── face? ──► match                 ~5-7s
      │ no                                                     at event end, only if ① and ② failed
      ▼
  dropped (and logged)
```

A fourth step, `hires_enroll`, is unrelated to *whether* someone is recognised — it fetches
a sharper copy of a face that is heading for the review queue, so the gallery gets better
reference photos. It is skipped when the face already came from ② or ③, which are
full-resolution to begin with.

## ① The snapshot, and why it so often fails

**Frigate picks its snapshot by highest *person* score — which is not the same as "a face
is visible".** Often the clearest view *of a person* is the moment they turn away. This is
not a rare edge case. Measured over seven days of real events on a four-camera setup:

| what the snapshot gave us | events | |
|---|---|---|
| no face detected at all | 43 | 68% |
| face too small (median 29px) | 7 | 11% |
| **usable face** | **13** | **21%** |

Two obvious suspects turned out to be innocent. **Night is not the problem** — relative to
how many events each produced, IR did no worse than daylight (9 usable out of 41 IR events,
4 out of 22 in colour). **Distance is not the problem either** — not one crop was narrower
than 120px; the people were plenty large in frame.

Run `scripts/why-no-face.py --days 7 --clip 12` to get this table for your own cameras.

## ③ Scanning the recording

*(Described before ②, because it came first and explains what ② improves on.)*

**Settings → Search the recording** (`clip_fallback`, default **on**). When an event ends
and the snapshot never produced a face, FaceID samples 12 frames across the clip and takes
the best one. Of twelve failed events re-checked frame by frame, **nine had a perfectly
good face** (det 0.68–0.87) that the snapshot simply missed.

**How much it helps depends entirely on where the camera points**, so measure yours rather
than expecting a number:

| camera | recording rescued |
|---|---|
| front door, at head height | 4/4 (and 9/12 in a wider sample) |
| child's room, mounted high | 0/4 |
| garden, zoomed | 0/2 |

Where people walk towards the lens, almost every failed snapshot is recoverable. Where the
camera looks down at people, or catches them side-on at distance, the clip holds no face
either — no amount of scanning invents one. Raising `det_size` does not change this: at
1280 instead of 640 the results were identical, at twice the cost (11.1s vs 5.4s per
event). The limit is the viewing angle, not the resolution.

Since the gain is so uneven, restrict it to the cameras that earn it:
`clip_fallback_cameras` (empty = all). On the setup above, limiting it to the front door
keeps every rescued event and drops roughly two thirds of the scans.

**The clip is not ready the instant an event ends.** Frigate finalises it a moment later,
while the finaliser reaches for it immediately — measured here, about one scan in four came
back with zero frames read. That used to be logged as "no face" and the event was dropped
because of a file that existed seconds later. A failed download is now retried
(`clip_fallback_retries`, default 3, every `clip_fallback_retry_seconds`, default 10);
a clip that *was* read and simply holds no face is not retried.

Three deliberate details:

* **Only when the snapshot found nothing.** Events that already worked cost nothing extra.
* **Selection is by detection quality, never by gallery similarity.** Picking whichever of
  twelve frames happens to look most like someone you know would inflate the numbers and
  invite misassignments. The frame is chosen on image quality; identity is decided
  afterwards, exactly as on the snapshot path.
* **Its own thread, short queue.** A clip scan takes seconds. It must not delay live
  recognition or presence updates, and during a burst of events it skips rather than
  building a backlog that runs minutes behind reality.

## ② When you need the answer *now*

The recording has one hard limit: it can only be scanned once an event has **ended**, and a
recorded moment is not retrievable until roughly **45 seconds** after it happened:

| requested age | result |
|---|---|
| 2s … 30s | nothing |
| 45s and older | frame, full resolution |

For "turn on the light when a known face arrives", that is far too late.

**Settings → Live hi-res retry** (`live_hires_fallback`, default **off**) closes the gap.
When the snapshot yields nothing, FaceID asks **go2rtc** — which ships with Frigate — for a
current frame of the *main* stream, in about a second. Frigate's own `latest.jpg` does not
help: it comes from the detect stream and is exactly as small as the snapshot.

Measured on a 2560x1920 camera, against the detect snapshot for the same events:

| source | events with a usable face | face size |
|---|---|---|
| detect snapshot | 5/8 | 50–105px |
| full-resolution frame | 7/8 | 104–212px |
| full-resolution + person-box crop | 3/8 | — |

Faces come out roughly twice as large, and events that produced nothing at all start
producing a face.

**The person-box crop is a trap.** Cropping the frame to Frigate's `data.box` first sounds
obvious — it is what the feature request proposed, and what I assumed too. It measured
*worse*: the box describes one specific moment, and by the time a frame is fetched the
person has moved out of it. Using the frame whole also removes the need to map
detect-stream coordinates onto the main stream.

**Everyone in the frame counts.** A full frame often holds more than one person (5 of 12
door events here), so every recognised face is published and added to the camera's presence
sensor — which has always tracked a set of people. Only the largest binds to the Frigate
event itself, since `sub_label` takes one name. This matters when a group arrives together:
Frigate raises one event per person, and whoever's snapshot fails would otherwise be lost,
even though their face is clearly in the frame fetched for someone else.

### Trigger mode: fallback or always

By default the live frame is a *fallback* — it runs only when the snapshot found nothing.
`live_hires_mode: always` turns it into the first step instead: Frigate merely signals
"someone is on this camera", and FaceID decides from the full frame how many faces are
actually there.

The case for it, raised in #8: if three people arrive and Frigate only tracks two because
one is partly hidden, the third never gets an event of their own. Both snapshots succeed,
the fallback never fires, and that person stays invisible — although their face is in the
frame.

**Measure before switching it on.** Over seven days on a front-door camera here: 59 events
in 31 groups, and in **0 of 15** analysable groups did the full frame hold more faces than
Frigate had reported events. The ratio ran the other way — five events for one visible
face — because Frigate re-tracks the same person as a new object when a track breaks. On
that setup `always` would have cost ~31 extra frame fetches a week and found nobody. Your
cameras may differ; a busier entrance with real groups is exactly where it could pay off.

Two safeguards:

* **A cooldown** (`live_hires_cooldown`, default 2s) per camera. One group produces a burst
  of events, and the frame would be practically identical for each — so it is fetched once.
* **Nothing found this way is bound to the Frigate event.** In `always` the scan runs
  *before* the snapshot has established who the event is about, so the largest face in the
  frame may well be somebody else. Those recognitions are published and counted towards
  presence, but `sub_label` and the event's own score stay with the snapshot.

It is off by default because go2rtc listens on port **1984**, which not every setup
exposes. FaceID probes it once at startup and says so either way:

```
go2rtc reachable (2560x1920) — 'live_hires_fallback' would work here
```

If instead it reports no answer, check that port 1984 is reachable from the FaceID
container and that the camera name matches go2rtc's `src`. Use `go2rtc_url` under
`frigate:` if it runs somewhere else.

## Small children are a hard case, not a tuning problem

Worth stating plainly, because no amount of threshold tuning fixes it: **a toddler's face
is roughly half the size of an adult's**. At the same distance from the camera it lands
below `min_face_px` while everyone else clears it comfortably. Measured on one household,
the rejected faces of a two-year-old clustered at **32–41px** against a limit of 48.

Three things compound:

* **Size.** The obvious one, and the reason `min_face_px` alone cannot be the answer —
  lowering it globally admits unreliable embeddings for everybody. Faces below roughly
  40px produce weak ArcFace vectors regardless of who they belong to.
* **Frigate rarely tracks them separately.** Small children are usually carried or walking
  right beside an adult, so they often do not get an event of their own. Over 195 events in
  one household, a two-year-old appeared as a candidate exactly twice.
* **The model was not trained for them.** ArcFace is built on adult faces. Toddler
  proportions differ, and at that age the face changes noticeably within months — reference
  photos go stale faster than you can collect them.

What actually helps is the **live hi-res frame**: it delivers faces roughly twice as large
as the detect snapshot, which is precisely the gap a small child falls into. Uploading
phone photos helps less than it does for adults, because a close-up portrait is not what
the camera sees from four metres away.

Set expectations accordingly. A two-year-old will not reach the reliability of an adult in
the same household, and that is a property of the problem rather than of the configuration.

## What it costs

Only failed events pay anything, and each stage runs at most once per event. With 63
events over seven days, 34 of them on the camera where both fallbacks are enabled:

| stage | events per week | added time |
|---|---|---|
| ① snapshot | all 63 | already there |
| ② live frame | ~21 (those without a snapshot face) | ~21s |
| ③ recording scan | ~3 (whatever ② missed) | ~18s |

Roughly **40 seconds of CPU per week**. Leaving the two badly-angled cameras out of both
fallbacks saves another ~190s per week for a measured zero recognitions.

The number to watch is not the total but *where* it lands: ① and ② share the worker thread
that handles events one after another, so a one-second live fetch briefly delays the next
event. At nine events a day that is irrelevant; during a burst it would add up. The
recording scan already runs on its own thread for that reason. Moving the live fetch there
too would cost the immediacy that makes it useful, so it stays in the worker until there is
evidence it hurts.

## Reading the log

The LOG tab (or `journalctl -u faceid -f`) narrates each stage:

```
event … (entrance): attempt 1, no face detected in snapshot 300x300
event … (entrance): live frame 2560x1920 has 2 face(s) the snapshot missed (largest 212px, det 0.78, 1.0s)
event … (entrance): no face in the recording either (12 frames, 1.7s)
Recording fallback limited to: entrance
Live hi-res fallback active via go2rtc (2560x1920)
```

The last two appear at startup. Without them, a fallback that never runs because of a
setting looks exactly like one that is broken.

## Related

* [Configuration reference](../README.md#configuration-reference) — every option
* [docs/example-config.yaml](example-config.yaml) — commented defaults
* `scripts/why-no-face.py` — the measurements above, for your own cameras
