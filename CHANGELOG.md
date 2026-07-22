# Changelog

All notable changes to FaceID. The Home Assistant add-on shows this file in the
update dialog; standalone users can watch GitHub releases.

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
- Optional HTTP Basic Auth for standalone installs (add-on uses HA ingress)
- Home Assistant add-on (amd64/aarch64, ingress, AVX pre-flight check)
