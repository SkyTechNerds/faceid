# Getting the name into your Frigate notification

Frigate's own notification fires before FaceID has a name, and reads a field FaceID does
not write. This is how to get the name in front of you anyway.

← back to the [README](../README.md)

If you use a Frigate notification blueprint (SgtBatten's is the common one), you have
probably noticed the name never appears in it. Two measured reasons:

- **The name is not ready yet.** The blueprint fires when the event starts; FaceID first
  needs a snapshot to exist, a face in it, and a match. Measured on one household setup over
  three days (49 recognitions) the name was ready after a **median of 9.9 seconds** — the
  fastest in 0.5 s, the slowest in 41.7 s.
  What sets the floor is Frigate's time to a first usable snapshot, and that varies a lot —
  it is not a fixed few seconds. Measure your own in FaceID's **Tools** tab (switch it on
  under Settings → *Does it actually work?*); the numbers depend on your cameras and your hardware.
- **Waiting can help — but often does not, for a different reason than I first published.**
  ⚠️ An earlier version of this section claimed Frigate never announces the name over MQTT.
  That was wrong: the test behind it ran against an *already finished* event, where nothing
  more is sent. On a **running** event, Frigate 0.17.2 forwards it in the same second, as
  `after.sub_label` = `["Eli", 0.514]` — an array of name and score.
  The catch is *where* it appears. At least one widely used Frigate notification blueprint
  reads `after.data.sub_labels`, which is `null` on 0.17.2. The name is in the payload, just
  not at the field being read — so the notification stays nameless and it looks like FaceID
  never wrote anything.

So do it the other way round — react to FaceID's own event and **replace** the notification
that already went out:

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FSkyTechNerds%2Ffaceid%2Fblob%2Fmain%2Fblueprints%2Ffaceid-name-the-person.yaml)

The trick is the **notification tag**: give it the same tag your Frigate blueprint uses (the
event id, by default) and the phone replaces the earlier message instead of stacking a
second one.

⚠️ **If the notification arrives without a picture**, the Frigate address is almost always
the reason. Since v0.20.0 the image comes from Home Assistant by default
(`/api/frigate/notifications/<event>/snapshot.jpg`), which the phone resolves against its
own HA connection — leave that setting alone and it works at home and away. If you switch to
a Frigate address instead, it has to be reachable **from the phone**: `http://ccab4aaf-frigate:5000`
and similar add-on hostnames exist only inside Home Assistant.

⚠️ **Already imported an older version?** Pasting the URL again does *not* replace it —
Home Assistant keeps the copy it has. Use **Settings → Automations & scenes → Blueprints →
⋮ → Re-import blueprint**.

## Or keep the blueprint you already have

Since Frigate does forward the name (see above), a Frigate-side blueprint can show it — it
just has to read `after.sub_label`, which on 0.17.2 is an array: `["Alice", 0.51]`, so the
name is `after.sub_label[0]`.

If you use SgtBatten's blueprint and would rather extend that than run a second automation,
**@crunchynuts has published a merged version** covering both routes — the Frigate payload
*and* `faceid/event`:

- [blueprint_sgtbatten_faceid](https://github.com/whoisdave/home-automations/blob/06ce3d77193b3bb054eafe97a5ea22a826d97231/blueprints/blueprint_sgtbatten_faceid_v0.5_good_20260831.yaml) ·
  [thread](https://community.home-assistant.io/t/1018333/51)

A community contribution, not maintained here: I have read it but not run it, and its
Signal notification path is written for the author's own setup. Which one to pick:

| | |
|---|---|
| You already run SgtBatten's blueprint and want its full feature set | take the merged version above |
| You want the name added with as little machinery as possible | take the blueprint in this repo |

The blueprint in this repo offers optional filters for cameras, Frigate zones, and whether
strangers should be announced at all — see
[blueprints/faceid-name-the-person.yaml](../blueprints/faceid-name-the-person.yaml).
