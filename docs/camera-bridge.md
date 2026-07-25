# Using your camera's own person detection

Most IP cameras run their own person detection on-device. It fires at moments Frigate
sometimes misses — someone approaching from an angle the detector struggles with, or a
brief appearance that never accumulates enough motion. A common trick is to bridge that
signal into Frigate: when the camera reports a person, an automation creates a Frigate
event for it.

FaceID can use those events too, but **not out of the box** — this page explains why,
how to check whether it's worth it for you, and how to set it up.

## Why FaceID ignores them by default

FaceID subscribes to `frigate/events`. Frigate publishes there for **tracked objects**:
things its own detector followed across frames. An event created through
`POST /api/events/<camera>/<label>/create` is not a tracked object — it is an entry
Frigate stores, clips and serves through its API, but never announces over MQTT.

So the bridge works, the events exist, Frigate shows them in Explore — and FaceID never
hears about them. On one installation that was 383 events in two weeks, all at the front
door, every single one unseen.

`poll_interval` closes that gap by additionally asking Frigate's event API what happened.

## Is it worth it for you?

Two things decide that: how many such events you get, and whether they contain faces.
Both are measurable before you change anything.

**How many:**

```bash
curl -s "http://frigate:5000/api/events?limit=500&after=$(($(date +%s) - 604800))" \
  | python3 -c "import sys,json,collections; e=json.load(sys.stdin); \
    b=[x for x in e if (x.get('data') or {}).get('type')=='api']; \
    print(len(b),'bridged events in 7 days'); \
    print(collections.Counter(x['camera'] for x in b))"
```

Note the `data.type` — the marker sits inside `data`, not at the top level.

**Whether they hold faces:** they are worth polling only if a face is actually visible.
The events carry a snapshot and usually a clip, so check a sample. On the installation
above, 12 of 20 held a usable face — 10 in the snapshot, 5 via the clip, some in both.
If your camera fires on people walking past at distance, your number will be lower.

## Setting up the bridge (Home Assistant)

Skip this if your bridge already exists. The camera's person sensor is exposed by most
integrations as a `binary_sensor`.

```yaml
# configuration.yaml
rest_command:
  frigate_person_event:
    url: "http://frigate:5000/api/events/{{ camera }}/person/create"
    method: POST
    content_type: "application/json"
    payload: >-
      {"source_type": "api", "sub_label": "Camera detection",
       "duration": null, "include_recording": true}
```

`duration: null` leaves the event open so the recording covers the whole approach; end it
explicitly, or set a fixed duration in seconds if you prefer fire-and-forget.

```yaml
# automations.yaml
- alias: "Camera person detection to Frigate"
  triggers:
      - trigger: state
        entity_id: binary_sensor.front_door_person
        to: "on"
  actions:
      - action: rest_command.frigate_person_event
        data:
          camera: entrance          # must match the camera name in Frigate's config
  mode: single
```

The `sub_label` is what you will see in Frigate's Explore view. FaceID overwrites it with
the recognised name once it identifies someone.

## Turning on polling

```yaml
faceid:
  poll_interval: 30      # seconds; 0 = off (default)
```

In the Home Assistant app the option carries the same name. Restart FaceID; the log then
shows each event it pulled in that MQTT never mentioned:

```
Poll: Ereignis 1785012921.911683-oxfvm1 (entrance) nachgezogen — von MQTT nie gemeldet
```

## What to expect, and what to watch out for

**Only finished events are polled.** Anything still in progress will be announced by MQTT
anyway if it is a tracked object, and a bridged event is worth processing once the
recording exists.

**On start, only the last two minutes are considered.** Polling does not backfill history
— use `python -m app.backfill --days 10` for that, which also covers bridged events.

**Snapshots are full frames, not person crops.** A bridged event has no bounding box, so
Frigate cannot crop to the person. Faces are therefore smaller relative to the image than
you may be used to. If nothing is ever recognised from bridged events, lower
`min_face_px` before blaming the bridge. The clip path (see
[Sharper reference photos](../README.md#sharper-reference-photos)) helps here: on that
same installation, faces went from 51–99 px in the snapshot to 134–231 px in the clip.

**Cost.** One API request per interval, plus normal recognition work per event found.
30 seconds is a good starting point; below 10 you are mostly adding load, since these
events are minutes apart anyway.

**Duplicates are handled.** Events already known from MQTT are skipped, and the poller
remembers the last 500 ids it has seen.

## A note on night-time

Bridged events cluster around the times your camera's own detection outperforms
Frigate's — often dusk and night. If your camera switches to infrared then, those faces
are greyscale, and a gallery built entirely from daylight photos will score them poorly.
`scripts/coverage.py` reports which cameras actually produce greyscale and who lacks a
reference for it. One night-time reference per person is usually enough to fix it.
