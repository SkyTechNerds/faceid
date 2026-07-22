# FaceID Add-on

Face recognition for [Frigate](https://frigate.video): recognized people are published
to MQTT (sensors appear automatically), written back to Frigate as `sub_label`, and
unknown faces land in a review UI (side panel) where you assign them with one click.

Full documentation: https://github.com/SkyTechNerds/faceid

## Setup

1. Set `frigate_url` to your Frigate instance (e.g. `http://192.168.1.10:5000`).
2. MQTT: leave `mqtt_host` empty to automatically use the Mosquitto broker add-on.
   Fill the `mqtt_*` options only for an external broker.
3. Optional: restrict processing to specific cameras (`cameras`), and list the cameras
   that should get a `sensor.faceid_<camera>` in Home Assistant (`discovery_cameras`).
4. Start the add-on. The first start downloads the recognition model (~300 MB) —
   check the add-on log until you see `MQTT verbunden`.
5. Open the **FaceID** panel in the sidebar. Recommended first step: run the backfill
   (see main README) or just wait — every detected unknown face shows up for review.

## Options

| Option | Description |
|---|---|
| `frigate_url` | Base URL of your Frigate instance |
| `mqtt_*` | Leave empty to use the internal Mosquitto add-on automatically |
| `match_threshold` | ≥ this cosine similarity = recognized (raise if strangers get misassigned) |
| `unknown_threshold` | < this = definitely unknown |
| `cluster_eps` | how aggressively unknown faces are grouped in the review UI |
| `presence_window` | camera sensor lists everyone seen within this many seconds |
| `set_sub_label` | write recognized names back to Frigate events |
| `cameras` | process only these cameras (empty = all) |
| `discovery_cameras` | cameras that get a Home Assistant sensor |

Face data (gallery, review queue) is stored in the add-on's data volume and survives
updates. Uninstalling the add-on deletes it.
