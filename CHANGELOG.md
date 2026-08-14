# Changelog

All notable changes to FaceID. The Home Assistant app shows this file in the
update dialog; standalone users can watch GitHub releases.

## 0.14.0 — 2026-08-14

- **New: references that make two people confusable are set aside automatically.** A hard
  profile, a blown-out IR shot, a steeply tilted head — such photos carry little real
  facial information, and ArcFace turns them into *generic* embeddings. Generic embeddings
  resemble each other regardless of who is in them, so keeping one for each of two people
  makes exactly those two harder to tell apart.
- This is not theoretical. On the gallery here the closest pair sat at **0.411** against a
  threshold of 0.45, and one of the two was in fact published under the other's name.
  Setting aside three photos (two IR night shots, one hard profile) dropped it to **0.368**
  and widened the margin from 0.039 to 0.052.
- Checked when a photo is added *and* on demand for the existing gallery
  (Settings → *Keeping the gallery clean* → **check gallery now**). The scan is iterative:
  after each removal the rest is re-scored, so it sets aside as few as possible — here 3
  rather than the 4 an isolated look suggested.
- **Nothing is deleted.** Photos move to the person's set-aside pile carrying the name they
  clash with and the reason, are marked there, and can be restored in one click. The
  judgement is statistical, not visual: a photo scoring 0.41 against someone else may still
  be a good likeness you have reason to keep, so the trade-off stays visible and reversible.
- The limit follows the threshold (`match_threshold - cross_risk_margin`, default margin
  0.05); `cross_risk_margin: -1` switches it off.
- **The Settings tab is grouped into sub-tabs** — Matching, No face?, Gallery, Does it
  work?, Backup. It had grown to eight sections in one column. The grouping happens after
  rendering, keyed on the section headings, so adding a setting still means writing one row
  and nothing else. The chosen tab is remembered.

## 0.13.1 — 2026-08-13

Three fixes from a detailed field report in #8 — all of them in the log rather than in
recognition itself, but a log that misleads is worse than a terse one.

- **"largest face 54px < min_face_px 48" was a contradiction, and it was ours.** The
  message only ever reported the *width*, while `best_face()` also requires the height and
  a minimum detection score. A 54x40px face failed on height, a 54x54px face with score
  0.40 failed on the score — both were reported as "too small". The line now names the
  actual reason: `largest face 54x40px, needs 48px on both sides`, or `is big enough, but
  detection score 0.40 < 0.55`.
- **A match below the threshold read like a recognition.** `match Kirill (0.251)` is
  written *before* the threshold is checked, so a weak candidate looked like a false
  identification — worrying if you drive automations off it. Nothing was ever published
  below `match_threshold`, but the log did not say so. It now ends in either `published` or
  `below threshold 0.45, NOT published — review queue`.
- **The occasional go2rtc miss is retried once.** Single failures were seen in two
  independent setups while the requests before and after went through; go2rtc has to grab a
  frame from a running stream, and a request landing between keyframes comes back empty.
  Dropping an event over that is a waste when a second attempt costs a fraction of a second.

## 0.13.0 — 2026-08-12

- **A person can be renamed.** Click the name on their card in the Persons tab. Until now a
  misspelling could only be fixed by deleting the person and starting over, which threw
  away every reference photo and every assignment made so far — a steep price for a typo.
  Reported in the community thread.
- Only the display name changes; photos, embeddings and assignments stay untouched. The
  internal folder keeps its original slug on purpose: it is referenced by stored files and
  image URLs, and a typo is no reason to move data around. The name is what is actually
  visible — in the UI, in the presence sensor and in the Frigate `sub_label`.
- Renaming to a name another person already has is refused with that reason, as is an empty
  name.

## 0.12.1 — 2026-08-10

- **Fixed: uploading a photo with several people in it could enrol the wrong face.** The
  upload took the *largest* face in the image, so a family photo where somebody else stood
  closer to the camera put **their** face into the person's gallery — silently, with the UI
  reporting a successful add. The effect is the opposite of what uploading is for: it makes
  two people harder to tell apart rather than easier.
- When more than one face is present, FaceID now picks the one that best matches the
  person's existing reference photos instead of the biggest. If that person has no
  references yet, or none of the faces resembles them (below 0.35), the photo is skipped
  with a message saying to crop it first — better than guessing.
- Single-face photos are unaffected, which is the normal case.

## 0.12.0 — 2026-08-10

- **New: `live_hires_mode: always`** — Frigate becomes only the trigger ("someone is on
  this camera") and FaceID decides from the full frame how many faces are present, instead
  of waiting for a snapshot to fail. Suggested in #8 for a case the fallback structurally
  cannot reach: if one of three arrivals is partly hidden, Frigate never tracks them as
  their own object, both other snapshots succeed, and that person stays invisible.
- **Measured before shipping, and the numbers argue against making it the default.** Seven
  days on a front-door camera: 59 events in 31 groups, and in **0 of 15** analysable groups
  did the full frame hold more faces than Frigate had reported events. It ran the other way
  — five events for a single visible face — because Frigate re-tracks a person as a new
  object when a track breaks. So `fallback` stays the default; `always` is there for setups
  where a busy entrance makes the case.
- A per-camera cooldown (`live_hires_cooldown`, default 2s) collapses the burst of events
  one group produces into a single frame fetch.
- In `always`, nothing found in the frame is bound to the Frigate event: the scan runs
  before the snapshot has established who the event is about, so the largest face may be
  someone else entirely. Those people are published and counted towards presence, while
  `sub_label` and the event score stay with the snapshot.

## 0.11.1 — 2026-08-10

- **Fixed: a recording scan that found nothing because the clip did not exist yet was
  counted as "no face".** Frigate finalises the clip a moment after an event ends, while
  the finaliser reaches for it immediately. The download then failed, `find_face_in_clip`
  returned empty-handed, and the event was discarded — over a file that would have been
  there seconds later.
- Visible in the log as an impossible duration: `no face in the recording either (12
  frames, 0.0s)`. A real scan takes 5–7s. Roughly **one scan in four** ended this way here,
  including the one that would have been the last chance to recognise someone at the door.
- The scan now reports how many frames it actually read, so "clip not ready" and "clip has
  no face" are told apart. The first is retried (`clip_fallback_retries`, default 3, every
  `clip_fallback_retry_seconds`, default 10); the second is not — it is a real answer.

## 0.11.0 — 2026-08-10

- **The live hi-res frame now recognises everyone in it, not just the largest face.**
  A full frame frequently holds more than one person — measured, 5 of 12 door events. Until
  now `best_face()` kept the biggest and discarded the rest, so whoever stood closest won.
- Frigate creates one event per person, so a group normally still works out. But when one
  of those events has no usable snapshot, that person was dropped — even though their face
  is plainly visible in the very frame we just fetched for someone else. Exactly the case
  from #8: several people arriving together, one recognised late or not at all.
- Every recognised person is published and counted towards the camera's presence sensor,
  which has always held a *set* of people. Only the largest face binds to the event
  (`sub_label` takes a single name, and `best_score` belongs to the person the event is
  about). Uncertain secondary faces are not queued for review — the queue should show the
  subject of the event, not passers-by in the background.
- Fixed along the way: the per-event "already announced" marker held a single name, so two
  known people in one frame would overwrite each other and be re-announced in turn — the
  duplicate notifications that marker exists to prevent. It is a set now.

## 0.10.1 — 2026-08-10

- **The header counters now update immediately after an action.** Assigning a cluster left
  "14 persons · queue 0 · events 0" showing the previous numbers until the 10-second
  refresh caught up — long enough to look like the assignment had not worked. The counters
  are refreshed as part of every re-render, so they move the moment anything changes.

## 0.10.0 — 2026-08-10

- **New: `live_hires_fallback` — recognition while it still matters.** The recording
  fallback from 0.9.0 can only run once an event has ended, and Frigate does not hand out
  a recorded moment until roughly **45 seconds** after it happened (measured). For
  automations that greet someone at the door, that is too late — reported in #8, where one
  person in a group was recognised promptly and another only long after.
- When the snapshot yields no usable face, FaceID now optionally asks **go2rtc** (which
  ships with Frigate) for a current main-stream frame — about **1 second**, at full camera
  resolution. Frigate's `latest.jpg` is no substitute: it comes from the detect stream and
  is exactly as small as the snapshot.
- Measured against the detect snapshot on the same events: usable faces went from **5/8 to
  7/8**, face size from 50–105px to 104–212px.
- The frame is used whole. Cropping it to Frigate's person box first — the obvious idea,
  and the one the issue suggested — measured **worse** (3/8): `data.box` describes a single
  moment, and by the time a frame is fetched the person has moved out of it.
- Off by default, because go2rtc listens on port 1984 and not every setup exposes it.
  FaceID probes it once at startup and logs the result either way, so the option is not a
  guess. Runs once per event, snapshot path first, and can be limited per camera
  (`live_hires_fallback_cameras`).

## 0.9.1 — 2026-08-09

- **`clip_fallback` can now be limited to specific cameras** (`clip_fallback_cameras`,
  empty = all; Settings → *…only these cameras*). 0.9.0 already documented that the gain
  depends almost entirely on the viewing angle — 4 of 4 events rescued at a front door at
  head height, 0 of 6 on a high-mounted indoor camera and a zoomed garden view — but there
  was no way to act on that. Scanning a camera nobody ever looks towards costs several
  seconds of CPU per event and finds nothing, every time.
- Restricting it to the cameras that earn it keeps every rescued event while dropping the
  scans that were never going to succeed. `scripts/why-no-face.py --clip` reports per
  camera, so the decision can be measured rather than guessed.
- Editable both as an app option and live in Settings; the service logs the restriction at
  startup, so a missing recording scan does not look like a defect.
- **Fixed a latent crash in the app's start script.** Camera lists were read as
  `.cameras | join(", ")`, and the `// empty` fallback in the config helper binds to
  `join()` rather than the lookup — so an option that is absent from `options.json` (which
  is exactly what happens the moment a new list option ships) aborted jq, and with `set -e`
  the app never started. All three lists now default before the pipe.

## 0.9.0 — 2026-08-08

- **Recognition now falls back to the recording when the snapshot has no face.** This is
  the single biggest limit on recognition — bigger than the gallery, the threshold or
  `match_top_k`, all of which took far more effort to tune.

  Frigate picks its snapshot by highest *person* score, which is not the same as "a face
  is visible" — frequently it is the moment someone turns away. Over seven days of real
  events only **21%** of snapshots held a usable face: 68% had no detectable face at all
  and 11% one below `min_face_px`.

  Re-checking twelve of the failed events against the clip found a good face in **nine**
  (det 0.68–0.87).

  How much this gains depends on where a camera points, so measure yours: at the front
  door, at head height, the recording rescued 4 of 4 events; on a high-mounted indoor
  camera and a zoomed garden view it rescued none — there the clip holds no face either.

  Three plausible explanations were measured and ruled out: night (IR yielded 9 usable
  faces out of 41 events, colour 4 out of 22 — no disadvantage), distance (not one crop
  was narrower than 120px), and detection resolution
  (`det_size` 1280 gave results identical to 640 at twice the cost). What remains is the
  viewing angle.

- Runs only when the snapshot found nothing, once per event, on its own thread with a
  short queue — live recognition and presence updates are never delayed, and a burst of
  events skips scans rather than building a backlog. New option **Search the recording**
  (`clip_fallback`, default on); turn it off on tight hardware.
- The frame is selected by detection quality, deliberately **not** by gallery similarity:
  picking whichever of twelve frames looks most like someone known would flatter the
  numbers and invite misassignments. Identity is decided afterwards, as on the snapshot
  path.
- Faces found this way skip the `hires_enroll` pass — they already come from the
  recording, so re-scanning the same clip would only cost time.

## 0.8.2 — 2026-08-08

- **Fixed: the app refused to start with "No MQTT broker configured" even with Mosquitto
  running.** The start script used `#!/usr/bin/env bashio`, but under s6-overlay v3 — what
  current Home Assistant base images use — that shebang does not receive the container's
  environment, so `SUPERVISOR_TOKEN` was missing and MQTT auto-detection could never work.
  It now uses `#!/usr/bin/with-contenv bashio` and additionally asks Home Assistant through
  the official `bashio::services` interface. Setting `mqtt_host` manually was the
  workaround; it is no longer needed.

## 0.8.1 — 2026-08-08

- **The app has an icon and a logo.** Home Assistant showed the default placeholder until
  now. The mark is a viewfinder bracket with three markers inside — one of them dimmed,
  for the face that is *not* identified yet, which is what this project is actually about.
  Deliberately no face, no eye, no scanning beam. Requested in the community thread.

## 0.8.0 — 2026-08-08

- **Measure recognition from the UI** (Settings → "Does it actually work?"). Until now the
  calibration analysis lived in `scripts/`, which needs shell access — so Home Assistant
  app users could change the threshold, `match_top_k` and `min_face_px`, but had no way to
  see whether it helped. That is the opposite of the advice this project gives everyone
  else. Closes #7.
- Two measurements, both without manual labelling: **leave-one-out** over the gallery
  (each reference photo tested against the gallery it was removed from — real ground
  truth) and a **probe over recent events** showing how much headroom each recognition
  has above the threshold.
- The result states the one number that decides whether the threshold can be lowered:
  **how high a stranger got**. Ignore anchors and cross-person matches both count. If any
  misassignment shows up, it says so instead — then the threshold goes up, not down.
- Events whose face is already in the gallery are excluded from the probe; they score
  ~1.0 and would flatter a low `top_k`.
- The analysis now lives in `app/analysis.py`, so UI and CLI compute identical numbers.

## 0.7.2 — 2026-08-07

- **Fixed: `min_face_px` was hard-coded in the Home Assistant app.** The app wrote a
  fixed `48` into its config regardless of any option, so the one setting that matters
  when faces arrive slightly too small could not be changed at all there. It is now an
  app option *and* live-editable in Settings, together with `det_size` and
  `max_attempts` — the other two knobs for exactly that problem. Reported in the
  community thread by someone whose detections sat just under the limit.
- **Saved backups are now listed and individually downloadable** (Settings). The download
  button always produced a *fresh* backup; the ones written by "backup now" or the daily
  auto-backup were unreachable from the UI — which, running as an app without filesystem
  access, meant unreachable full stop. The list also shows at a glance whether the
  auto-backup is actually running.
- **Backups download instead of opening as gibberish on iOS.** Safari ignores
  `Content-Disposition` for `application/gzip` and renders the archive inline; the
  download now uses `application/octet-stream` plus a `download` attribute.

## 0.7.1 — 2026-08-02

- **Select and delete ignore anchors across all groups.** Each group already had its own
  delete button, but with many groups that meant clicking through them one by one.
  The Ignored tab now has *select all*, *clear* and *delete selected* spanning every
  group, with a confirmation that spells out the consequence.
- **New: `max_ignore_anchors`** (Settings, 0 = unlimited) — a cap on *auto-learned*
  anchors per group, mirroring the per-person photo cap. Requested as "delete ignored
  people after N days", but age is the wrong criterion here: an anchor from three months
  ago is exactly as valid as yesterday's, and deleting it lets that person resurface in
  the review queue — the opposite of what the ignore list is for. The cap drops the most
  **redundant** anchor instead, and never touches one you added by hand.

## 0.7.0 — 2026-07-27

- **Optional authentication against Frigate.** Set `user`/`password` under `frigate:` and
  FaceID logs in against Frigate's authenticated API (port 8971), keeping the session and
  renewing it on 401. Without credentials nothing changes — the open port 5000 keeps
  working exactly as before, and a failed login falls back to unauthenticated instead of
  breaking recognition.
- **Measured, not assumed:** a Frigate `viewer` account can read everything FaceID needs,
  but **cannot** write `sub_label` — Frigate answers `Role viewer not authorized.
  Required: admin`. So authenticating either costs you the name write-back into Frigate,
  or requires admin credentials in a config file. The README states both plainly rather
  than recommending one; a rejected write now logs exactly that instead of a bare 403.
- `verify_tls` (default false) because Frigate ships a self-signed certificate — enabling
  verification without your own CA would fail every request.
- All Frigate calls now go through one client. The history scan, the startup check, the
  camera list and the poller previously issued their own unauthenticated requests, which
  would have quietly bypassed any login.

## 0.6.13 — 2026-07-26

- **Log messages are English now.** README, UI, docs and changelog were English while the
  service logged in German — so anyone reporting a problem had to translate their own log
  first, and the diagnostic lines added in 0.6.10–0.6.12 were unreadable for most of the
  people they were written for. All user-facing log output is now English. (Code comments
  stay German; they are for whoever edits the source, not for whoever runs it.)

## 0.6.12 — 2026-07-26

- **"No face found" now says which kind.** The log distinguishes *a face was detected but
  it is too small* (reporting its pixel width, the `min_face_px` limit and the snapshot
  dimensions) from *no face at all* — two very different problems. Too small points at
  Frigate's `snapshots.height` or camera distance; none at all points at viewing angle or
  light. Previously both produced the same line, leaving nowhere to start.

## 0.6.11 — 2026-07-26

- **New LOG tab.** The service log is now visible in the web UI — the last 500 lines,
  refreshing every 5 seconds, with a warnings-only filter and a copy button for pasting
  into a bug report. Home Assistant app users had the app's log tab; standalone and
  container users had to reach for `journalctl` or `docker logs`, which is exactly the
  wrong moment to switch to a terminal when you are trying to find out why nothing is
  being recognised. Noise from the inference library is filtered out.

## 0.6.10 — 2026-07-26

- **Fixed: polled events could be processed twice.** The finalizer clears an event from
  memory once it is done, so the poller then saw a fully processed MQTT event as new and
  ran it again — while logging the untruth "never announced by MQTT". It now remembers
  the last 1000 event ids it handled.
- **The log no longer goes silent when nothing is recognised.** Events without a usable
  face are the normal case (back to camera, too far away), but they were dropped without
  a word — making a healthy install look identical to a broken one. Both "no snapshot"
  and "no face >= min_face_px" are now logged. Measured here: over 19 hours, 17 of 20
  events held no face at all, and the log said nothing about any of them.

## 0.6.9 — 2026-07-26

- **Fixed: no Home Assistant entities unless you listed your cameras.** An empty
  `cameras` list means "process every camera" — but MQTT discovery looped over exactly
  that empty list, so it announced nothing. Anyone running the default configuration got
  no sensors at all. FaceID now asks Frigate for the camera list when none is configured,
  and additionally announces a camera the first time it sees an event from it. Reported
  in the community thread; it never showed up here because our own config lists cameras
  explicitly.
- **New: `frigate_topic_prefix`** (default `frigate`). The subscription was hard-coded,
  so a Frigate instance with a custom `mqtt.topic_prefix` was silently never heard.
- **Startup now says whether Frigate is reachable**, lists its cameras, and warns about
  configured cameras that do not exist there. "It recognises nothing" and "nothing ever
  arrives" were indistinguishable in the log before.

## 0.6.8 — 2026-07-25

- **Hotfix: the web UI stayed blank after 0.6.6.** The new "photos averaged per match"
  field declared a `const tk` that already existed for the set-aside limit — a
  `SyntaxError` that stops the entire script from loading, so the whole page died, not
  just Settings. CI now runs `node --check` over the inline script, so a broken UI can
  no longer be released.
- **Fixed: the header showed `queue 0` while faces were waiting.** It reported the size
  of the internal processing queue, not the review queue. The review count is now what
  it says it is; the internal one moved to `processing`.

## 0.6.7 — 2026-07-25

- **New: `poll_interval`** — optionally also poll Frigate's event API instead of relying
  on MQTT alone. Events created through Frigate's API are not tracked objects and never
  appear on `frigate/events`, so FaceID never saw them. A common case is a camera's own
  person detection wired up as a reliability bridge: on one installation that was ~22
  events per day at the front door, of which 12 in 20 held a usable face — roughly
  doubling the events FaceID could learn from. Off by default; 30 seconds is sensible.
- Polled events run through the same pipeline. They carry no bounding box, so their
  snapshot is the full frame rather than a person crop — face detection copes, and the
  clip path (sharper reference photos) applies as usual.

## 0.6.6 — 2026-07-25

- **`match_top_k` is now adjustable in Settings** ("Photos averaged per match"). It was
  config-file only, yet it turns out to be the single biggest lever on recognition — and
  the sensible value depends on your gallery.
- Measured on a real 128-photo gallery (leave-one-out, so with ground truth), lowering it
  from 3 to 1 nearly doubled correct recognitions (36 → 65) with **zero** misassignments,
  and *widened* the margin to the runner-up. The reason: the score averages the k best
  matching photos, so a person whose references cover many different angles gets dragged
  down by her own less similar photos — punishing exactly the well-covered people.
- The practical probe now excludes **self-hits**: an event whose face is already in the
  gallery scores ~1.0 and measures nothing. It flattered small k badly — after excluding
  them on a k-independent basis (highest single similarity, not the k-mean), the honest
  comparison on one identical test set is 90% / 93% / 100% recognised for k = 3 / 2 / 1.
- The imbalance concern behind top-k was checked, not assumed: strangers (ignore anchors)
  peaked at 0.19 against a 0.50 threshold at every k, and no wrong person ever crossed
  the threshold. Measure your own with `scripts/measure-recognition.py` before changing.

## 0.6.5 — 2026-07-25

- **Reference photos now remember where they came from.** Assigning a face used to keep
  only the crop and the embedding, throwing away camera and event time — so it was
  impossible to tell whether somebody was enrolled from one camera only. `meta.json`
  now carries a `sources` entry per photo (existing photos stay as they are, marked
  unknown).
- **coverage.py reports camera spread** and flags "only ever seen at one camera", which
  matters more than photo count: a different camera means a different angle, height and
  light.
- **Fixed a misleading warning.** "No night shot" was reported for every person, but
  where motion-triggered lights switch on, the camera records in colour at night and no
  IR frame ever exists. Missing IR shots are now only flagged for cameras that actually
  produce greyscale, derived from the gallery instead of assumed.

## 0.6.4 — 2026-07-25

- **New: `scripts/coverage.py`** — per person, how well is she actually covered and what
  is missing? Reports photo count, diversity (mean pairwise similarity), viewing angles
  estimated from the landmarks (frontal / half / profile), day vs night/IR shots, and a
  leave-one-out self test — then names the concrete gap ("no frontal shot", "no night
  shot", "too few photos").
- **New: `scripts/measure-recognition.py`** — did enrolling actually help? Compares the
  current gallery against an older one (an unpacked backup) via leave-one-out, plus a
  practical probe against recent Frigate events.
- The self test is deliberately harsh and pessimistic on small galleries — with five
  varied photos, each has to hold up against four entirely different situations. It is
  only reported as a defect from eight photos up, and a low value means "these photos
  reinforce each other weakly", not "this person is not recognised in practice".

## 0.6.3 — 2026-07-25

- **Fixed: review cards showed the wrong date.** A face was stamped with the moment
  FaceID processed it, not the moment it happened — so a history scan over four weeks
  labelled every card with today's date. Cards now carry the Frigate event time and the
  UI prefers it. Existing cards can be repaired with
  `python scripts/backfill-event-ts.py` (pulls the real time via the stored event ID).

## 0.6.2 — 2026-07-25

- **History scan can recover missed events** (`python -m app.backfill --rescue`). When the
  detect snapshot holds no usable face, the event clip is scanned instead — measured over
  180 events, about one in five yields a face that the normal scan misses entirely.
- **Quality gate calibrated against real data, not intuition.** A first run put 308 faces
  into the review queue, most of them useless: back-of-head shots, motion blur, and
  outright false positives (a church spire scored 0.57). Sorting 74 finds by detection
  score showed a clean split — below ~0.8 it is mostly junk, above it mostly real faces.
  Rescue therefore requires **0.85** by default (`--rescue-min-det`), which cuts the yield
  to roughly a seventh and leaves the usable finds.
- Deliberately **no** filter on gallery match score: a stranger's face scores low by
  definition, and enrolling strangers is what the queue is for. Also no pose filter —
  measurement showed working galleries are full of profile shots (median frontality 0.55),
  so filtering those would discard good references.

## 0.6.1 — 2026-07-25

- **Sharper reference photos now actually land.** 0.6.0 sampled a handful of timestamps
  from the recording and hoped one of them held the face — on real events that worked
  only about one time in six, because Frigate picks its snapshot from a moment we cannot
  know. FaceID now pulls the event clip once and scans frames across it instead: same
  ~2x larger faces, but four times as often (measured 4/6 vs 1/6 on the same events).
- **Fixed: the wrong face could veto a good frame.** With several people in view, only
  the *largest* face in a frame was compared against the original — if that was somebody
  else, the whole frame was discarded even though the right person was in it. All faces
  in a frame are now checked and the best identity match wins.

## 0.6.0 — 2026-07-23

- **Sharper reference photos (new default)**: faces entering the review queue — live and
  via the history scan — are now re-fetched from Frigate's *recording* instead of the
  downscaled detect stream. Across a dozen real events faces came out about twice as
  large (84px → 178px), which means better recognition and far clearer separation between
  real duplicates and same-person-other-angle. Candidate frames are sampled across the
  event and each must match the original face, so with several people in frame the wrong
  one cannot be enrolled. Live recognition keeps using the fast snapshot path. Toggle:
  **Settings → Sharper reference photos** (`hires_enroll`).

## 0.5.6 — 2026-07-23

- **Honest hover highlight.** Hovering a set-aside photo used to outline its three closest
  matches in green and claim they were duplicates — misleading, since a photo removed for
  the photo limit is merely similar (same person, other angle), not a duplicate. Now the
  photo it actually duplicates is outlined bright green, merely-similar photos get a grey
  outline, and **every marked photo shows its similarity %** so you can tell the two
  apart. The tooltip text matches the real reason it was set aside.

## 0.5.5 — 2026-07-23

- Hotfix: a broken code path made /api/persons return 500 right after 0.5.4 (the
  set-aside partner was referenced before being read). Person list works again.

## 0.5.4 — 2026-07-23

- **Detects true duplicate images**, not just similar faces: a perceptual-hash pass now
  finds photos whose *image* is identical to another one (e.g. the same crop stored
  twice). These could never be caught by face-similarity — for the model they look like
  two different faces — which is why visibly identical tiles survived earlier passes.
- **Fixed the hover highlight on set-aside photos**: it now always highlights the photos
  it was considered a duplicate of (the exact partner is recorded when trimming, plus the
  closest matches), instead of silently showing nothing when similarity fell below a fixed
  threshold.

## 0.5.3 — 2026-07-23

- Hover-highlight on set-aside photos now uses the same sensitivity as duplicate removal,
  so hovering a trimmed photo highlights only its genuine near-duplicates — not every
  same-person photo. Previously the 0.45 highlight lit up most of a person's gallery on
  noisy camera crops, making everything look like a duplicate.

## 0.5.2 — 2026-07-23

- **Duplicate removal now works on camera data**: small low-res crops make even
  near-identical faces score only ~0.66 similar (a phone photo would be 0.95+), so the
  old 0.75 floor never triggered. The sensitivity range is now 0.50–0.95 (default 0.65)
  with a **live preview** of how many photos would be set aside as you adjust it.

## 0.5.1 — 2026-07-23

- **Remove duplicates** (Settings): scans all persons and sets aside photos that are
  near-identical to one you already have — they add nothing to recognition. Adjustable
  duplicate sensitivity; the more redundant of each pair is moved (restorable). Diversity
  is what makes recognition robust, not photo count. Fixed lingering 'add-on' wording in
  the UI (Home Assistant calls them apps).

## 0.5.0 — 2026-07-23

- **Set-aside photos no longer grow unbounded**: FaceID now keeps only the most recent
  `trimmed_keep` trimmed photos per person (default 10, adjustable in Settings) and
  deletes older ones, plus a **clear all** button per person. Recognizing a face live
  never adds to the gallery — only manual assign/upload does — so day-to-day use costs
  no extra storage. See docs/trimming.md.

## 0.4.9 — 2026-07-23

- Hovering a set-aside (trimmed) photo now highlights the active reference photos it is
  most similar to — so you can see at a glance which photos it was considered a duplicate
  of.

## 0.4.8 — 2026-07-23

- Added a dedicated **docs/trimming.md** explaining the photo-limit behaviour in depth
  (why, the exact most-redundant selection rule with numbers, restore/curate workflow),
  linked from a "Learn more" link in the set-aside section and from the README.

## 0.4.7 — 2026-07-23

- Trimmed (set-aside) photos are now clearly distinguished: shown desaturated and dimmed
  under a "SET ASIDE" label, and full-colour on hover, so they read as archived rather
  than active reference photos.

## 0.4.6 — 2026-07-23

- Clearer Settings: the save button is now "SAVE SETTINGS" (it saves the thresholds and
  the photo limit together) — the photo-limit field only applies when you press it.

## 0.4.5 — 2026-07-23

- **Adjustable photo cap in Settings** (`max_faces_per_person`): lowering it trims every
  person down to the new limit immediately (most-redundant photos set aside, restorable),
  so the trimming behaviour is easy to see and control. Documented in the README.

## 0.4.4 — 2026-07-23

- **Transparent photo trimming**: when a person exceeds the photo limit, the removed
  reference is no longer silently deleted — it is set aside and shown on the person card
  with a short reason (most similar to the rest, so diverse angles are kept), plus
  one-click **restore** or delete. The eviction already preferred redundant over unique
  photos; now you can see and undo it.

## 0.4.3 — 2026-07-23

- **Self-healing gallery**: on startup, persons whose reference filenames collided in
  pre-0.2.1 data (duplicate names, embedding/image count mismatch) are repaired
  automatically — filenames become unique and 1:1 with embeddings, so backups and the
  UI stay consistent. Recognition data is never touched.

## 0.4.2 — 2026-07-23

- Fix: the Download-Backup link and file-upload labels now match the button styling
  (the .act/.ghost styles only applied to <button> before).

## 0.4.1 — 2026-07-23

- **New Settings tab** — matching thresholds (recognition, unknown, suggestion, cluster,
  ignore) are now live-editable sliders, and backup/restore lives here instead of on the
  Persons tab. Edits are stored in `data/settings.json` and override config / app
  options, so they persist across restarts and updates.
- **Built-in daily auto-backup** (optional): enable it, choose the hour and how many to
  keep — runs inside FaceID, no external cron needed. App options and a documented
  host-cron / HA-automation alternative included.

## 0.4.0 — 2026-07-23

- **Backup & restore**: download your whole gallery (persons + ignore anchors) as a
  `.tar.gz` from the Persons tab, and restore it later — either **replace** everything
  or **merge** in only what's missing. Path-traversal-safe. Your face data is the one
  irreplaceable thing here, so now it's one click to safeguard.
- **Removed the Recognitions tab** — it was an in-memory, since-restart-only list that
  never persisted; Frigate's Explore (with the names FaceID writes back) covers "who
  was seen when" far better.

## 0.3.2 — 2026-07-23

- **Configurable suggestion threshold** (`suggest_threshold`, default 0.40): controls
  when an unknown face is grouped into a "Looks like <person>" suggestion. Available as
  an app option too.
- **Jump-to-person dropdown** on the Persons tab (shown once you have more than a few
  people) — pick a name to scroll straight to that person.

## 0.3.1 — 2026-07-23

- **Smarter review queue**: unknown faces that resemble an enrolled person are now
  grouped into one **"Looks like <name>"** card with a single **ASSIGN ALL** button —
  no more assigning the same person cluster by cluster. The person dropdown is
  pre-selected to the suggestion, and each face has a ✗ **"not this person"** button to
  pull it out if it doesn't belong. Remaining unrecognized clusters keep their own
  dropdown, pre-selected to the best guess.
- **Grouped person dropdowns**: every person picker is now split into ★ Favorites and
  Others, alphabetically sorted.

## 0.3.0 — 2026-07-23

- **Favorites & sorted person list**: mark people as favorites with the ★ button on
  their card. The Persons tab now groups into **Favorites** and **Others**, each sorted
  alphabetically — so the household members you care about stay at the top.

## 0.2.9 — 2026-07-23

- **Fix: tab content race** — switching tabs while a fetch was still in flight
  could let the finishing request overwrite the newly opened tab (e.g. freshly
  detected unknown faces appeared under the Ignored tab). Each view now only
  renders if its tab is still active.

## 0.2.8 — 2026-07-22

- **Fix: fresh installs and updates crashed on start** (`ImportError:
  find_face_padded`) — a helper in `engine.py` (padded retry for close-up portrait
  detection, used by photo upload and CLI enrollment) was missing from the published
  sources. Thanks @KoenvanH for the report (#1). The release process now runs an
  import-consistency check so this class of error can't ship again.

## 0.2.7 — 2026-07-22

- Release an ignored group directly into a **new** person: the Ignored tab got the
  same "…or new name" field the unknown queue has.

## 0.2.6 — 2026-07-22

- Tooltips on every button, icon and control — tab navigation, cluster actions,
  ignore-group curation, backfill and person management all explain themselves
  on hover now.

## 0.2.5 — 2026-07-22

- **Curatable ignore groups**: anchors now carry a persistent group (existing anchors
  are migrated automatically). You can merge groups, move selected anchors between
  groups, and assign wrongly ignored faces directly to a real person — auto-learned
  anchors join the group of their best-matching anchor, so groups converge on real
  identities over time. Select tiles for partial actions, or apply to a whole group.

## 0.2.4 — 2026-07-22

- **Ignored tab groups anchors by person** (same clustering as the unknown queue),
  with per-group actions: restore a whole group to review or delete it. Groups show
  auto-learned counts and the original person name for "ignore person" entries.

## 0.2.3 — 2026-07-22

- **Ignore anchors now learn**: when an ignored person reappears with a changed look,
  the new appearance is added as an additional anchor automatically — so they stop
  resurfacing in the unknown queue over time. Guardrails: only on unambiguous matches
  (similarity ≥ ignore_threshold + 0.1 AND a clear margin over every enrolled person),
  near-duplicate anchors are skipped, and auto-learned anchors are visibly marked
  "auto" in the Ignored tab (delete anytime). Disable with ignore_learning: false.

## 0.2.2 — 2026-07-22

- Ignored faces now live in their own **IGNORED tab** instead of a section at the
  bottom of the Unknown tab.

## 0.2.1 — 2026-07-22

- **"Ignore person" button** on person cards: stop tracking an enrolled person in one
  click — all their reference faces become ignore anchors (reversible via the Ignored
  section). No more manual unassign-then-ignore round trips.
- Fix: reference filenames could collide when many faces were added within the same
  millisecond (bulk uploads), silently overwriting each other.

## 0.2.0 — 2026-07-22

- **Ignore list**: the "ignore" action on unknown faces now keeps the face as a
  negative anchor — an ignored person is never notified, never matched to a known
  person and never resurfaces in the review queue. No more dummy persons for people
  you simply don't want to track. Manage them in the new "Ignored" section
  (restore to review or delete). "Discard" remains for garbage crops.
- **Fairer matching**: person score is now the mean of the top-k (default 3) most
  similar reference images instead of the single best one — a person with many
  photos no longer wins borderline matches on a lucky outlier. Note: absolute
  scores drop slightly; if known people start landing in review, lower
  `match_threshold` a notch.
- **Per-person photo cap** (default 40): adding more drops the most redundant
  reference, keeping galleries balanced.
- New config options: `match_top_k`, `max_faces_per_person`, `ignore_threshold`.

## 0.1.6 — 2026-07-22

Initial public release.

- Face recognition for Frigate person events (InsightFace `buffalo_l`:
  SCRFD detection + ArcFace embeddings, CPU-only)
- Review UI: auto-clustered unknown faces (DBSCAN), one-click assignment,
  bulk "apply suggestions", full-snapshot lightbox, move faces back to review
- One-click camera history scan (backfill) with live progress
- Photo upload and CLI folder enrollment; robust detection for close-up portraits
- Frigate write-back: `sub_label` on live recognitions, retroactively via the
  history scan, and when assigning a face in the review UI
- Home Assistant: MQTT discovery sensors per camera with presence window
  (`Alice, Bob` → `nobody`), `faceid/event` topic for automations
  (exactly one message per Frigate event and person)
- Configurable MQTT topic prefix/client id for multi-instance setups
- Optional HTTP Basic Auth for standalone installs (app uses HA ingress)
- Home Assistant app (amd64/aarch64, ingress, AVX pre-flight check)
