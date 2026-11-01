# FaceID App

Face recognition for [Frigate](https://frigate.video): recognized people are published
to MQTT (sensors appear automatically), written back to Frigate as `sub_label`, and
unknown faces land in a review UI (side panel) where you assign them with one click.

Full documentation: https://github.com/SkyTechNerds/faceid

## Setup

1. Set `frigate_url` to your Frigate instance (e.g. `http://192.168.1.10:5000`).
   **No Frigate?** Leave it empty and use folder mode instead (below).
2. MQTT: leave `mqtt_host` empty to automatically use the Mosquitto broker app.
   Fill the `mqtt_*` options only for an external broker.
3. Optional: restrict processing to specific cameras (`cameras`), and list the cameras
   that should get a `sensor.faceid_<camera>` in Home Assistant (`discovery_cameras`).
4. Start the app. The first start downloads the recognition model (~300 MB) —
   check the app log until you see `MQTT verbunden`.
5. Open the **FaceID** panel in the sidebar. Recommended first step: run the backfill
   (see main README) or just wait — every detected unknown face shows up for review.

## Options

| Option | Description |
|---|---|
| `frigate_url` | Base URL of your Frigate instance |
| `go2rtc_url` | go2rtc for the live hi-res frame; empty = Frigate's host, port 1984. Set it when Frigate sits behind a reverse proxy |
| `go2rtc_user` / `go2rtc_password` | only when go2rtc has `api.username`/`password`; without them it answers `401` |
| `mqtt_*` | Leave empty to use the internal Mosquitto app automatically |
| `match_threshold` | ≥ this cosine similarity = recognized (raise if strangers get misassigned) |
| `unknown_threshold` | < this = definitely unknown |
| `cluster_eps` | how aggressively unknown faces are grouped in the review UI |
| `presence_window` | camera sensor lists everyone seen within this many seconds |
| `set_sub_label` | write recognized names back to Frigate events |
| `cameras` | process only these cameras (empty = all) |
| `discovery_cameras` | cameras that get a Home Assistant sensor |
| `suggest_threshold` | score at which unknown faces are grouped into a "looks like <person>" suggestion |
| `min_face_px` | smallest face (pixels wide) FaceID will try to recognise. Lower it if the log keeps saying "largest face NNpx < min_face_px"; below ~40 px ArcFace gets unreliable |
| `det_size` | detector input size; higher finds smaller/further faces at the cost of CPU |
| `max_attempts` | recognition attempts per event — more attempts means more chances to catch the moment the face is largest |
| `max_faces_per_person` | photo cap per person; the most redundant reference is set aside when exceeded (restorable) |
| `trimmed_keep` | how many set-aside photos to keep per person (0 = delete immediately) |
| `dedupe_threshold` | default sensitivity for the Settings "Remove duplicates" action |
| `hires_enroll` | fetch new review-queue faces from the recording instead of the detect snapshot (sharper references) |
| `frigate_topic_prefix` | must match `mqtt.topic_prefix` in Frigate's own config (default `frigate`). Wrong value = FaceID hears nothing at all |
| `poll_interval` | seconds; >0 also polls Frigate's event API for events MQTT never announces (e.g. events created by an automation from a camera's own detection). 0 = off |
| `backup_enabled` / `backup_hour` / `backup_keep` | optional built-in daily gallery backup |
| `backup_dir` | where that backup is written. Empty = inside the app's data volume. Set `/share/faceid` to put it where Home Assistant's own backups will pick it up. The path is write-tested at start-up and when you save it in Settings — `/media` is mounted read-only, so a backup target under it is refused instead of failing every night |
| `cross_risk_margin` | how close two people's references may get before one is set aside (relative to `match_threshold`; 0 or below = off) |
| `self_outlier_ratio` | sets aside a reference photo that fits its own person far worse than the rest (0 = off) |
| `history_keep` | how many recognitions the History tab keeps (0 = history off) |
| `folder_*` | watch a directory instead of (or alongside) Frigate — see below |

## Folder mode — watch a directory instead of Frigate

For setups without Frigate: point FaceID at a folder of finished recordings or snapshots.
Everything else stays the same — gallery, unknown review, history, MQTT sensors.

```yaml
frigate_url: ""
folder_enabled: true
folder_path: /media/faceid
folder_camera: front_door
folder_extensions: [".jpg", ".jpeg", ".png", ".webp"]
```

⚠️ **Only `/media` and `/share` are visible to the app**, mounted read-only — FaceID never
changes or deletes the files it watches. A path under `/config` or on the host will not be
found, and the log says so on start.

⚠️ **Still images need their extension listed.** The default `folder_extensions` is
video-only, and the option *replaces* that list rather than adding to it. A folder of
`.jpg` is skipped in silence until you list it.

A camera that keeps overwriting the same file works: FaceID fingerprints by size and
modification time, not by name. One `folder_camera` applies to the whole folder, so
several cameras writing into one directory all report under that single sensor.

| Option | Description |
|---|---|
| `folder_enabled` | switch the directory watcher on |
| `folder_path` | directory to watch, under `/media` or `/share` |
| `folder_camera` | name used for the MQTT sensor and in the history |
| `folder_recursive` | also watch subdirectories |
| `folder_process_existing` | process what is already there on first start (off = only new files) |
| `folder_extensions` | which file types to pick up — replaces the default video-only list |
| `folder_poll_interval` | seconds between directory scans |
| `folder_settle_seconds` | a file must be this old and unchanged across two scans before it is read |
| `folder_max_frames` | frames sampled per video |
| `folder_max_people_per_file` | cap on distinct faces kept from one file |
| `folder_max_indexed_files` | cap on the "already seen" index (0 = unlimited). Keep it above the number of files the folder holds, otherwise every scan re-reads the overflow |

Thresholds and backup can also be changed live on the app's **Settings** tab; those
edits are stored in the app's data volume and override these options. The Settings tab
additionally holds **"photos averaged per match"** (`match_top_k`) — a match score is the
mean of that many best-fitting reference photos. Higher resists a single lucky photo;
lower helps people whose references cover many different angles, since their own less
similar photos otherwise drag the mean down.

## Backups

Face data (gallery, review queue) is stored in the app's data volume and survives
updates. Uninstalling the app deletes it.

The recognition model cache is **excluded from Home Assistant backups** — roughly 600 MB
that re-downloads by itself on the next start. Nothing in it can be lost.

The gallery is the one thing that cannot be re-created. Set `backup_dir` to `/share/faceid`
and switch on `backup_enabled`: the daily archive then lands where Home Assistant's own
backups already look, at a few MB.

⚠️ **The app image is still stored in every backup** (~1.4 GB), because the app is built
locally on your machine rather than pulled from a registry. Excluding it is not possible
from here — that needs prebuilt images, tracked in
[#33](https://github.com/SkyTechNerds/faceid/issues/33).
