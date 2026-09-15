# FaceID — self-hosted face recognition for Frigate + Home Assistant

FaceID is a small, self-hosted service that adds **reliable, trainable face recognition**
on top of [Frigate](https://frigate.video). It uses the same model family as Immich and
CompreFace (**InsightFace `buffalo_l`**: SCRFD detection + ArcFace embeddings) and was
built because Frigate's built-in face recognition UX didn't cut it:

FaceID can also run without Frigate by watching a directory of completed recordings.
It samples frames from each new video, groups repeated views of the same face within the
clip, and sends each distinct face through the normal gallery, unknown-review, history,
and MQTT pipeline. Processed file fingerprints survive restarts, and input files are
opened read-only and never changed.

- **No train-tab treadmill.** Matching is nearest-neighbor over face embeddings — every
  image you assign is a visible reference point, with no training cycles and no queue
  that refills with already-known faces. To be clear: this is not immune to bad data —
  an imbalanced or mislabeled gallery still degrades matching (a person with many
  reference images wins borderline matches more often). The difference is that the
  failure mode is an image you can see and delete, not an opaque model update.
- **Strangers are first-class.** Unknown faces are collected, **auto-clustered** (DBSCAN,
  the same trick photo apps use) and reviewed in a web UI: one click assigns a whole
  cluster to a person — or **ignores** it. People you enroll (the mailman you *want*
  notifications for) get recognized; people you ignore go permanently silent.
- **An ignore list that actually sticks.** Ignored faces stay as *negative anchors*:
  never notified, never matched to your family, never resurfacing in review — and
  FaceID learns their new looks over time (only on unambiguous matches with a clear
  margin over every enrolled person, visibly marked "auto", deletable anytime).
  Anchors are grouped per person; groups can be merged, curated, or released into a
  real person with one click if you change your mind.
- **Train from anywhere, without bloat.** Assign faces from your cameras, upload photos
  from your photo library, or enroll whole folders via CLI. A per-person photo cap keeps
  galleries lean: when it's exceeded, the reference **most similar to the rest** is set
  aside (so unusual angles are kept, not lost) — visibly, on the person card, where you
  can restore it. The cap is adjustable in the Settings tab.
- **Home Assistant native.** MQTT discovery sensors per camera (presence-window state like
  `Alice, Bob` → `nobody`), plus a per-recognition event topic for automations — and a
  [notification blueprint](blueprints/faceid-name-the-person.yaml) that puts the recognised
  name into the Frigate notification you already get, by *replacing* it once the name is
  known — independent of which Frigate version puts the name where, and carrying the score
  and zones with it. ([Why a Frigate-side blueprint often shows no name](#getting-the-name-into-your-frigate-notification).)
- **Tags flow back to Frigate.** Recognized names are written as `sub_label`, so you can
  filter clips by person in Frigate's Explore view — including retroactively: the history
  scan tags past events, and assigning a face in the review UI tags its original event too.
- **Yours to keep.** A Settings tab holds the matching thresholds (live-editable) plus
  one-click **backup & restore** of your gallery, and an optional built-in **daily
  auto-backup** — your hand-curated face data is the one irreplaceable thing, so it's
  easy to safeguard.

## Contents

**What it is** — [Screenshots](#screenshots) · [How it works](#how-it-works) · [Local-only, and what gets downloaded](#local-only-and-what-gets-downloaded)

**Setting it up** — [Requirements](#requirements) · [Install as a Home Assistant app](#install-as-a-home-assistant-app-recommended-for-haos) · [Install standalone](#install-standalone-lxc-vm-bare-metal) · [Connecting to Frigate](#connecting-to-frigate) · [Recording folder instead of Frigate](#completed-recording-folder-mode-without-frigate) · [Getting started](#getting-started)

**Living with it** — [Ignoring people](#ignoring-people) · [Sharper reference photos](#sharper-reference-photos) · [How training stays healthy](#how-training-stays-healthy) · [Backup & restore](#backup--restore)

**When it does not recognise someone** — [When the snapshot has no face](#when-the-snapshot-has-no-face) · [Events MQTT never announces](#events-mqtt-never-announces) · [Calibrating the threshold](#calibrating-the-threshold) · [Seeing what it is doing](#seeing-what-it-is-doing) · [Measuring instead of guessing](#measuring-instead-of-guessing)

**Home Assistant** — [Home Assistant](#home-assistant) · [Getting the name into your Frigate notification](#getting-the-name-into-your-frigate-notification)

**Reference** — [Updates](#updates) · [Security & privacy notes](#security--privacy-notes) · [Configuration reference](#configuration-reference)

**In depth** — [Tuning the threshold](docs/tuning-the-threshold.md) · [Measuring](docs/measuring.md) · [Notification name](docs/frigate-notification-name.md) · [Recognition pipeline](docs/recognition-pipeline.md) · [Connecting to Frigate](docs/frigate-connection.md) · [Gallery trimming](docs/trimming.md) · [Camera bridge](docs/camera-bridge.md)


## Screenshots

*All faces below are AI-generated (StyleGAN) — no real persons.*

**Unknown review** — new faces arrive auto-clustered; assign a whole cluster with one
click, or scan your camera history to bootstrap the gallery:

![Unknown review with auto-clustered faces](docs/screenshot-unknowns.png)

**Persons** — your gallery; upload photos, rename a person by clicking their name, or send
faces back to review:

![Person gallery](docs/screenshot-persons.png)

## How it works

```
Frigate --MQTT frigate/events--> FaceID
   ^                                |  snapshot.jpg?crop=1 (person crop)
   |                                v
   +--API sub_label---------- InsightFace buffalo_l -> cosine match vs. gallery
                                    |
        match >= 0.50 -> publish person + tag Frigate event
        below         -> review queue (clustered in the web UI)

MQTT -> Home Assistant:  sensor.faceid_<camera>  +  faceid/event (JSON)
```

## Local-only, and what gets downloaded

FaceID performs **all recognition locally on your hardware** — no cloud APIs, no
accounts, no telemetry. The only thing ever fetched from the internet is the open-source
recognition model itself, once, on first start:

- **What:** InsightFace `buffalo_l` model pack (SCRFD face detection + ArcFace
  recognition, the same open models Immich and CompreFace use)
- **From where:** the official [InsightFace GitHub release](https://github.com/deepinsight/insightface/releases/tag/v0.7)
- **Size:** ~300 MB, cached on disk afterwards (survives restarts and app updates)

After that download, FaceID works completely offline. Your camera images and face data
never leave your machine.

## Requirements

- Either Frigate 0.16+ with MQTT, or a readable directory of completed recordings.
  MQTT remains optional in folder mode; enable it only when Home Assistant discovery and
  recognition events are wanted.
- A CPU with AVX (any Intel/AMD from the last decade; no GPU needed).
  **Running HAOS/your host in a VM?** The default virtual CPU model (e.g. Proxmox `kvm64`)
  hides AVX — set the VM CPU type to `host` and cold-restart the VM, or the recognition
  runtime will refuse to start.

### What it actually uses

Measured on an idle-to-normal household setup (4 cameras, ~60 events a week):

| | |
|---|---|
| RAM, steady state | **~650 MB** |
| RAM, peak | ~1.0 GB |
| CPU per snapshot | ~0.2 s at `det_size: 640` |
| Disk | ~300 MB models + your gallery (a few MB) |

**Most of that RAM is the recognition model, and it is not tunable.** Loading `buffalo_l`
accounts for **572 MB** of it — the libraries themselves (Python, numpy, opencv,
onnxruntime) come to only 64 MB. Within that, the ArcFace model `w600k_r50` alone costs
**333 MB**: it is a ResNet-50 whose ~25M parameters are unpacked into memory as float32.
FaceID already skips the three model files it does not need (3D landmarks, 106-point
landmarks, gender/age), which would add another ~140 MB.

Things that sound like they should help but were measured and do **not**: thread count
(`OMP_NUM_THREADS` 1/2/4), `det_size`, and disabling onnxruntime's CPU memory arena. All
three left the figure unchanged. Anyone reporting ~1 GB is seeing normal behaviour, and
tools built on the same models (Immich, CompreFace) sit in the same range.

**`det_size` is the CPU lever, not a RAM lever.** Same memory, very different speed:

| `det_size` | time per image |
|---|---|
| 640 (default) | ~185 ms |
| 480 | ~107 ms |
| 320 | ~60 ms |

Lower values find small and distant faces less reliably, so trade it against the
*Why do events yield no face?* tool (Tools tab, or `scripts/why-no-face.py`) rather than
by feel.

If ~650 MB is genuinely too much, the only real reduction is a smaller model family
(`buffalo_s`, MobileFaceNet instead of ResNet-50, roughly a third of the memory) at the
cost of recognition quality — and switching would invalidate every stored embedding, so
the whole gallery would need re-enrolling.

## Install as a Home Assistant app (recommended for HAOS)

*(Apps were formerly known as apps.)*

1. Add this repository to your app store — one click:

   [![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FSkyTechNerds%2Ffaceid)

   (or manually: **Settings → Apps → App Store → ⋮ → Repositories** → add
   `https://github.com/SkyTechNerds/faceid`)
2. Install the **FaceID** app, set your Frigate URL in the options (MQTT is picked up
   automatically from the Mosquitto app) and start it.
3. Open the **FaceID** panel in the sidebar. First start downloads the model (~300 MB).

The app is built locally on your machine (amd64/aarch64). See
[faceid-addon/DOCS.md](faceid-addon/DOCS.md) for all options.

## Install standalone (LXC, VM, bare metal)

Tested on Debian 12/13 and Ubuntu 22.04+; any Linux with Python 3.10+ works.

**1. System packages**

```bash
apt install python3-venv python3-dev build-essential libglib2.0-0 libgl1 libxcb1 libgomp1
```

**2. Get the code and install the Python environment**

```bash
git clone https://github.com/SkyTechNerds/faceid /opt/faceid
cd /opt/faceid
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**3. Configure**

```bash
cp docs/example-config.yaml config.yaml
nano config.yaml   # set: Frigate URL, MQTT host + credentials, your camera names
```

**4. Run as a service**

```bash
cp faceid.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now faceid
```

**5. Verify**

The first start downloads the model pack (~300 MB, one time — see above). Follow the
log with `journalctl -u faceid -f` until you see `MQTT verbunden (Success)`, then open
`http://<host>:8600` and check that the header shows your person/queue counters.

## Connecting to Frigate

By default FaceID uses Frigate's API on **port 5000**, which is unauthenticated — fine on
a trusted LAN, and it needs no configuration. Frigate also serves an authenticated API on
port 8971:

```yaml
frigate:
  url: https://192.168.1.10:8971
  user: faceid
  password: secret
  verify_tls: false     # Frigate's default certificate is self-signed
```

One thing to know before you switch, measured rather than assumed: a `viewer` account can
read everything FaceID needs, but **cannot write `sub_label`** back into Frigate
(`Role viewer not authorized. Required: admin`). So authenticating costs you either the
names in Frigate's Explore view, or requires admin credentials in a config file.

Trade-offs, TLS, what FaceID actually requests, and which setup fits which network:
**[docs/frigate-connection.md](docs/frigate-connection.md)**.

## Completed-recording folder mode (without Frigate)

Contributed by [@thethereza](https://github.com/thethereza) — the idea and the implementation are his. FaceID began as a Frigate companion, but the recognition, gallery, unknown review and history were never Frigate-specific; only the source of the images was.

Set `frigate.enabled: false` and add a `folder` section to `config.yaml`:

```yaml
frigate:
  enabled: false
  url: ""

folder:
  enabled: true
  path: /recordings
  camera: front_door
  poll_interval: 10
  settle_seconds: 10
  max_frames: 24

mqtt:
  enabled: false  # enable later if Home Assistant notifications are wanted
```

Only completed files with configured extensions are considered. A file must keep the
same size and modification time over two polls and be older than `settle_seconds`.

**Still images work as well as video** — `.jpg`, `.jpeg`, `.png` and `.webp` run through
the same face pipeline, which matters if your recorder saves a snapshot per motion event
rather than a clip. They are **not** in the default `extensions` list, though, so a folder
of images is silently skipped until you add them:

```yaml
folder:
  # This key REPLACES the default, so keep the video formats you want as well
  extensions: [.mp4, .mkv, .mov, .avi, .webm, .m4v, .jpg, .jpeg, .png, .webp]
```

The default stays video-only on purpose: pointing FaceID at a folder that also holds
thumbnails or wallpapers should not quietly enrol half of them.

Successful files are indexed in `data/folder_ingest.json`, so restarts do not create
duplicate sightings. Replacing a file at the same path gives it a new fingerprint and
processes it again. The Unknown-tab button becomes **Scan recording folder**, and the
header shows the number of processed files.

## Getting started

1. **Scan your camera history** (optional but recommended): in the Unknown tab, click
   **"Scan camera history"** — faces from past Frigate events land in the review queue,
   pre-clustered per person, and already-known people are tagged in Frigate retroactively.
   (CLI alternative: `venv/bin/python -m app.backfill --days 14`)
2. **Assign clusters** in the UI (Unknown tab): pick a name per cluster — select individual
   tiles first if a cluster contains a stray face. The ⛶ button shows the full snapshot
   for context.
3. Once a few people exist, use **"apply suggestions"** to bulk-assign everything the
   gallery already recognizes with ≥ 50 % similarity. Repeat as the gallery grows.
4. Optionally upload 5–10 clear photos per person (Persons tab) as clean anchors. Photos
   with several people are handled: FaceID picks the face matching that person's existing
   references rather than the largest one, and skips the photo if none of them does. Or
   enroll a folder: `venv/bin/python -m app.enroll "Alice" /path/to/photos`.

## Ignoring people

Not everyone deserves a notification. FaceID distinguishes three actions on an unknown
face, and the difference matters:

| Action | Meaning |
|---|---|
| **Assign** | This is someone I track — recognize, notify, tag in Frigate |
| **Ignore** | I know who this is and never want to hear about them — silent forever |
| **Discard** | Garbage crop (blurry, not a face) — delete, no memory kept |

Ignored faces become negative anchors, grouped by person in the **Ignored** tab:

- Reappearances are silently dropped (a log line is all you get), and genuinely new
  looks are **auto-learned** into the right group — guarded so a household member can
  never silently become an anchor (requires high similarity *and* a clear margin over
  every enrolled person; auto anchors are marked and deletable). Opt out with
  `ignore_learning: false`.
- **Curate groups**: merge two groups when they're the same person, move selected
  anchors between groups, or send a face back to the review queue.
- **Change your mind**: release a whole group into an existing or brand-new person —
  the anchors become that person's reference gallery and tracking starts immediately.
- You can also ignore an entire enrolled person via **"ignore person"** on their card.

**Training tips:** camera snapshots beat photo-library images (same lens, angle and light
as at recognition time). Diversity beats volume. Create dedicated persons for regular
strangers (mailman, neighbors) instead of discarding them — that keeps them from being
force-matched to your family.

## Sharper reference photos

Frigate runs detection on a **downscaled** stream (often 1280x960 or even 640x360) but
records in **full camera resolution** (e.g. 2560x1920). Snapshots come from the detect
stream, so faces arrive smaller and softer than they need to be.

With **Settings → Sharper reference photos** enabled (default), a face heading for the
review queue is re-fetched from the *recording* instead: measured across a dozen real
events, faces came out roughly **twice as large** (e.g. 84px → 178px). Better references
mean better recognition — and, as a bonus, similarity scores spread out, making genuine
duplicates easy to tell from "same person, other angle".

Details: FaceID downloads the event clip once and scans frames across it, because Frigate
picks its snapshot from a moment that can't be queried afterwards. Every candidate face
is compared against the snapshot face and the best identity match wins — so with several
people in frame, the wrong face can't be enrolled. When no frame yields a usable face
(roughly one event in three), the original snapshot is kept.

Live recognition tries the fast snapshot path first; only when that yields nothing does it
fall back to a full-resolution frame — see
[docs/recognition-pipeline.md](docs/recognition-pipeline.md). Needs recordings enabled for
the camera.

## When the snapshot has no face

Frigate picks its snapshot by highest *person* score, which is not the same as "a face is
visible" — often it is the moment someone turns away. Measured over seven days of real
events, only **21%** of snapshots held a usable face.

FaceID therefore falls back, in order, and only when the step before found nothing:

| | source | when | cost |
|---|---|---|---|
| ① | Frigate snapshot | every event | already there |
| ② | live full-resolution frame via go2rtc | immediately, opt-in (`live_hires_fallback`) | ~1s |
| ③ | scan of the event recording | at event end (`clip_fallback`, on by default) | ~5–7s |

On the setup measured here this lifts usable events from a fifth to about four in five —
but the gain depends almost entirely on the **camera angle**, both fallbacks can be
limited per camera, and the live one needs go2rtc reachable on port 1984.

**→ [docs/recognition-pipeline.md](docs/recognition-pipeline.md)** covers the whole thing:
the measurements behind each stage, why cropping to the person box makes things *worse*,
how several people in one frame are handled, what it costs, and how to read the log.

### Recovering missed events

Sometimes the detect snapshot holds no usable face at all and the event is skipped
entirely. The history scan can go back over those via the clip:

```bash
python -m app.backfill --days 28 --rescue
```

About one in five such events yields a face this way. Because there is no snapshot face
to check identity against, the only guard is detection quality — and it has to be strict:
in a measured run, finds below ~0.8 were overwhelmingly back-of-head shots, motion blur
and false positives (a church spire scored 0.57), while finds above it were real faces.
The default is `--rescue-min-det 0.85`; lowering it multiplies the queue faster than it
adds usable references.

Expect a clip download (8–32 MB) and several seconds per affected event, so this is a
manual run, not something the live pipeline does.

**Keep `--days` within your recording retention.** Rescue needs the clip, so events older
than `record.retain` yield nothing and only cost time — a 28-day run against a 10-day
retention spends most of its hour on events it cannot help.

## Events MQTT never announces

FaceID listens on `frigate/events`. Events **created through Frigate's API** are not
tracked objects, so they never appear there — FaceID simply never learns about them.

This matters if you use a camera's own person detection as a reliability bridge: an
automation sees the camera report a person and creates a Frigate event for it. Those
events carry a snapshot and usually a clip, but stay invisible to FaceID.

Set `poll_interval` (seconds, e.g. `30`) to also poll Frigate's event API:

```yaml
faceid:
  poll_interval: 30
```

On one installation that added ~22 events a day at the front door, of which 12 in 20
held a usable face — roughly doubling what FaceID could learn from. Polled events run
through the same pipeline; they carry no bounding box, so their snapshot is the full
frame rather than a person crop. Off by default, since it costs one API request per
interval.

Setting up such a bridge, checking beforehand whether it pays off, and the pitfalls of
box-less snapshots: **[docs/camera-bridge.md](docs/camera-bridge.md)**.


## Calibrating the threshold

`match_threshold` is the one number that decides between "a name arrives" and "a stranger
gets someone else's name". Raising it does not simply make FaceID stricter about
strangers — in practice the people you enrolled are the ones who fall below the line
first.

**→ [docs/tuning-the-threshold.md](docs/tuning-the-threshold.md)** — what the number
trades away, how to find yours from your own data, and why strangers are rarely the limit.

## Seeing what it is doing

The **LOG tab** shows the last 500 log lines straight in the UI — including the quiet
cases that decide whether a setup works: whether Frigate answers at startup, which
cameras were announced to Home Assistant, and for every event whether a face was found
at all. If nothing is ever recognised, that tab usually says why within a few lines.
There is a warnings-only filter and a copy button for pasting into an issue.

## Measuring instead of guessing

Nothing in this project is tuned by feel. Three reports answer the questions people
actually have — *how fast is it?*, *is my gallery good enough?*, *why did that event
produce no face?* — and they run either in the UI or from a terminal.

**→ [docs/measuring.md](docs/measuring.md)** — the Tools tab, the standalone scripts, and
how to read what they tell you.

## How training stays healthy

Recognition is only as good as the reference photos, so FaceID keeps galleries diverse
rather than large:

- **A successful recognition never adds a photo.** Recognising you at the door tags the
  Frigate event and updates the sensor — but the gallery stays untouched. This is
  deliberate: a gallery that grows from its own matches reinforces whatever it already
  believes, and a single wrong match would quietly breed more of the same. References
  only come from what *you* assign in the review queue, or upload yourself.

- **Ignore anchors can be capped too** (`max_ignore_anchors`). With `ignore_learning`
  switched on, every unambiguous ignore match adds an anchor, which grows without bound
  on a busy street. The cap drops the most redundant auto-learned anchor per group —
  never a manual one, and never by age, since an old anchor is exactly as valid as a new
  one and removing it would let that person resurface.
- **New photos are only kept if they add something** — a near-duplicate of one you
  already have is skipped.
- **A per-person cap** (default 40, adjustable in Settings) bounds how many references a
  person keeps. When exceeded, FaceID sets aside the photo that is **most similar to all
  the others** — i.e. the most redundant one — so a rare side/angle shot is preserved
  while a 30th near-identical front shot is the first to go.
- **Remove duplicates on demand**: even under the cap, near-identical photos add nothing.
  **Settings → Remove duplicates** finds both truly identical *images* (perceptual hash) and
  near-identical *faces* (embedding similarity) and sets them aside (live preview),
  keeping the gallery diverse — as a button, so you stay in control. (Camera crops score
  lower than phone photos, so useful sensitivity is ~0.60–0.70.)
- **Nothing vanishes silently**: trimmed photos appear on the person card with a short
  reason and a one-click **restore** (or delete), and the set-aside pile is itself capped
  (`trimmed_keep`) so it never grows without bound.

If someone is recognized poorly from a certain angle, just add a photo from *that* angle —
being unusual, it's automatically kept.

**Full details:** [docs/trimming.md](docs/trimming.md) explains the why, the exact
selection rule (with numbers), and how to restore or curate set-aside photos.

## Backup & restore

Your gallery (enrolled persons + ignore anchors) is the one thing you can't regenerate —
so FaceID makes it easy to keep. Everything is on the **Settings** tab:

- **Download backup** — a `.tar.gz` of `persons/` + `ignored/` (not the unknown queue).
- **Restore** — *replace* everything, or *merge* in only what's missing (handy for moving
  people between instances).
- **Automatic daily backup** — enable it, pick an hour and how many to keep. It runs
  inside FaceID (no external cron needed); the folder defaults to `data/backups`, which
  survives app updates. Point it at a mounted share to get backups off the box.

**Automate it yourself** if you prefer: the download is a plain endpoint, so any host
cron or Home Assistant automation can pull it:

```bash
# nightly host cron — keep 14 days
curl -fsS http://<faceid-host>:8600/api/backup -o /backups/faceid-$(date +\%F).tar.gz
find /backups -name 'faceid-*.tar.gz' -mtime +14 -delete
```

Settings changed here (thresholds + backup) are stored in `data/settings.json` and
**override `config.yaml` / app options**, persisting across restarts and updates.

## Home Assistant

Sensors appear automatically via MQTT discovery (`sensor.faceid_<camera>` for every
camera in `discovery_cameras`). The state lists everyone recognized within
`presence_window` seconds (`Alice, Bob`), then falls back to `nobody`. Attributes carry
the person list and the last recognition (score, event id).

For automations, trigger on the `faceid/event` topic — one JSON message per
(Frigate event, person): see [docs/ha-automation-example.yaml](docs/ha-automation-example.yaml)
for a phone-notification automation with the Frigate snapshot attached.

```json
{"person": "Alice", "score": 0.612, "camera": "driveway",
 "event_id": "1786945313.350301-xiec8d", "ts": 1786945318.4,
 "zones": ["driveway_zone"]}
```

`zones` lists the Frigate zones the person entered during that event, so an automation can
tell *where* someone was seen — notify for strangers in the driveway, stay quiet for the
street, ignore known faces anywhere. The same payload appears as the `last` attribute on
the presence sensor, where it stays until the next recognition replaces it — the state
falls back to `nobody` when nobody is around, but `last` keeps answering who was seen there
most recently. It is absent until the first recognition after a restart.

**So the sensor reads `nobody` most of the time, and that is normal** — it only holds a name
while someone was seen in the last `presence_window` seconds, and a Frigate event lasts a few
of those. If you look at the topic between events, you will see `nobody` with `last` still
naming whoever passed by. A background sweep every 5 seconds clears expired names, so the
switch to `nobody` happens up to 5 seconds after the window itself ran out. Raising
`presence_window` makes people count as present for longer; it changes nothing about
recognition. This is the reason automations should trigger on `faceid/event` rather than poll
the sensor: the event fires at the moment of recognition, the sensor answers "is anyone there
right now".

⚠️ **An empty `zones` does not mean "outside every zone".** It also appears when the camera
has no zones at all, and when Frigate never registered the person in one — being *visible*
in a region and being *counted as in a zone* are different things (the zone test uses the
object's anchor point and has its own timing rules). On the gallery here, 12 of 28 correct
recognitions on a zoned camera carried no zone. Treat empty as *unknown*, not as *elsewhere*,
and decide deliberately what your automation does with it. If you want zone gating to be
strict, do it in Frigate instead — `snapshots: required_zones:` stops the event from ever
reaching FaceID, because FaceID only acts on events that have a snapshot.

## Getting the name into your Frigate notification

Frigate's own notification fires the moment a person is detected — seconds before FaceID
has a name — and the common blueprints read a field that stays empty even once it exists.
Both are fixable.

**→ [docs/frigate-notification-name.md](docs/frigate-notification-name.md)** — the
blueprint that replaces the sent message, and how to adapt an automation you already have.

## Updates

- **Home Assistant app:** you get notified automatically when a new version is available
  (Settings → Apps → FaceID → Update). The changelog is shown right in the update dialog.
- **Standalone:** `cd /opt/faceid && git pull && systemctl restart faceid`. Watch the
  GitHub releases to get notified.

See [CHANGELOG.md](CHANGELOG.md) for the full history.

## Security & privacy notes

- The web UI supports optional **HTTP Basic Auth** (`faceid.auth` in config.yaml) —
  strongly recommended for standalone installs; the HA app is protected by ingress
  and your Home Assistant login instead. Either way: it manages biometric data — keep
  it on a trusted LAN and don't expose port 8600 to the internet (Basic Auth without
  TLS is not internet-grade protection).
- All face data stays in `data/` on your host (JPEG crops + embeddings). Delete a person
  and their data is gone.
- Depending on where you live, informing household members/visitors about face
  recognition on your cameras may be legally required. Be a good human.

## Configuration reference

See [docs/example-config.yaml](docs/example-config.yaml) — every option is commented. The two knobs
that matter most:

| Option | Meaning |
|---|---|
| `match_threshold` (0.50) | raise if strangers get matched to known persons, lower if known persons end up in the review queue |
| `cluster_eps` (0.55) | raise to merge unknown clusters more aggressively, lower if different people land in one cluster |
| `match_top_k` (3) | a person's score is the mean of their top-k reference similarities — dampens photo-count bias (1 = raw max) |
| `max_faces_per_person` (40) | soft cap; adding more drops the most redundant reference (0 = unlimited) |
| `cross_risk_margin` (0.05) | a reference closer than `match_threshold` minus this to **another** person is set aside — such photos make two people confusable ([details](docs/trimming.md#references-that-make-two-people-confusable)); `-1` disables the check |
| `self_outlier_ratio` (0.25) | a reference whose average similarity to the **same person's** other photos falls below this share of that person's median is set aside — such a photo holds almost no face information and attracts strangers ([details](docs/trimming.md#references-that-do-not-resemble-their-own-person)); needs ≥5 photos, `-1` disables |
| `history_keep` (200) | how many published recognitions to keep in the **HISTORY** tab, each with the face image actually used and the embedding behind it — that is what makes the *was wrong* analysis possible; `0` disables it |
| `ignore_threshold` (= match_threshold) | similarity at which a face counts as ignored |
| `ignore_learning` (true) | learn new looks of ignored people as additional anchors (guarded) |
| `hires_enroll` (true) | fetch new review-queue faces from the recording (sharper references) |
| `clip_fallback` (true) | when a snapshot yields no face at all, scan the recording — on most setups the single biggest gain, see [the pipeline doc](docs/recognition-pipeline.md) |
| `clip_fallback_cameras` (all) | restrict the fallback to cameras where it actually pays off — the gain depends on the camera angle |
| `live_hires_fallback` (false) | when the snapshot fails — no face in it, or Frigate not producing one at all — ask go2rtc for a full-resolution frame right away instead of waiting for the event to end. Reports every face in that frame, not just the largest ([details](docs/recognition-pipeline.md)) |
| `live_hires_fallback_cameras` (all) | restrict that to specific cameras |
| `live_hires_mode` (fallback) | `always` scans the full frame on every event instead of only after a failed snapshot — measure first, it found nothing extra here |
| `clip_fallback_frames` (12) | how many frames to sample from the clip |
| `clip_fallback_retries` (3) | retries when the clip is not finalised yet — without them roughly one scan in four is lost |
| `clip_fallback_min_det` (0.65) | detection score a clip frame must reach — stricter than the snapshot path, because there are twelve frames to choose from |
| `poll_interval` (0) | seconds; >0 also polls Frigate's event API for events MQTT never announces |

## License

MIT
