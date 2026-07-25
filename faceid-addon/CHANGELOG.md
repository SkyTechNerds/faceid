# Changelog

All notable changes to FaceID. The Home Assistant app shows this file in the
update dialog; standalone users can watch GitHub releases.

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
