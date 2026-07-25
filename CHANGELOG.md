# Changelog

All notable changes to FaceID. The Home Assistant app shows this file in the
update dialog; standalone users can watch GitHub releases.

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
