# Connecting to Frigate: open port or authenticated?

Frigate exposes its API twice: unauthenticated on **port 5000**, and behind a login on
**port 8971** (HTTPS). FaceID works with either. This page lays out what actually differs
— measured against Frigate 0.17, not inferred from the docs.

## The short version

| | Port 5000 (default) | Port 8971 + credentials |
|---|---|---|
| Setup | none | Frigate account + 3 config lines |
| Reading events, snapshots, clips | ✅ | ✅ |
| Writing `sub_label` back | ✅ | **only with an admin account** |
| Anyone on the network can query it | yes | no |
| Credentials stored on disk | none | yes, in `config.yaml` |
| Transport | plain HTTP | HTTPS (self-signed by default) |

If your Frigate is reachable only inside a trusted LAN, port 5000 is a perfectly
defensible choice and needs no configuration at all. It is the default for that reason.

## What FaceID asks Frigate for

Useful to know when judging how much access it actually needs:

| Call | Purpose | Required |
|---|---|---|
| `GET /api/config` | camera list, startup check | read |
| `GET /api/events` | history scan, optional polling | read |
| `GET /api/events/<id>/snapshot.jpg` | the face to recognise | read |
| `GET /api/events/<id>/clip.mp4` | sharper reference photos | read |
| `POST /api/events/<id>/sub_label` | writing the recognised name back | **write** |

Only the last one writes anything, and only that one is a problem with a restricted
account.

## Using the authenticated port

```yaml
frigate:
  url: https://192.168.1.10:8971
  user: faceid
  password: secret
  verify_tls: false     # see the TLS note below
```

Create the account in Frigate under **Settings → Users**. FaceID logs in at startup,
keeps the session, and renews it automatically when a call comes back `401`. If the login
fails, it logs a warning and continues unauthenticated rather than stopping — a wrong
password degrades the connection, it does not break recognition.

## The catch: a viewer account cannot write

This is the part worth knowing before you set it up. A Frigate `viewer` account reads
everything FaceID needs, but writing `sub_label` is rejected:

```
403  {"detail":"Role viewer not authorized. Required: admin"}
```

Frigate checks for the admin role specifically — this is not a granular permission that a
custom role can be given. So authenticating leaves you with a genuine trade-off:

**Admin account.** Everything works, including names appearing in Frigate's Explore view.
The cost: admin credentials sit in a config file, and whoever can read that file can
reconfigure your entire Frigate.

**Viewer account plus `set_sub_label: false`.** Recognition, Home Assistant sensors and
the review workflow all work; you only lose the names inside Frigate. FaceID logs a clear
warning if a write is rejected, so this never fails silently.

There is no third option that gives you both a restricted account and the write-back.

## A detail that surprised us

The role check only applies to the authenticated port. On port 5000 a `sub_label` write
is accepted **even when FaceID is logged in as a viewer** — the open API does not enforce
roles at all. Practically: setting credentials while staying on port 5000 changes nothing
except an extra login at startup. It also means the security gain comes from *not exposing
port 5000*, not from authenticating on it.

## TLS

Frigate ships a self-signed certificate. With `verify_tls: true` every request fails
unless you have imported Frigate's CA, which is why the default is `false` — the
connection is still encrypted, it is just not verified against a trusted authority. If you
run Frigate behind a reverse proxy with a real certificate, turn verification on.

Be sceptical of advice that recommends the authenticated port *and* certificate
verification in the same breath without mentioning this: on a stock Frigate the
combination simply does not work.

## Which should you pick?

Genuinely depends on your threat model, and neither is strictly safer:

- **Everything on one trusted LAN, nobody else on the network** — port 5000 is fine, and
  the simplest thing that works.
- **Shared or untrusted network, guests, IoT VLAN** — authenticate, and decide whether the
  write-back is worth an admin account. If in doubt, use a viewer and drop `sub_label`;
  losing the names in Frigate is a smaller loss than handing out admin rights.
- **Frigate reachable from the internet** — then port 5000 should not be exposed at all,
  regardless of what FaceID does. Fix that first.

Whatever you choose, the startup log states plainly what happened: whether Frigate
answered, whether a login succeeded, and — if a write is refused — exactly which account
lacks which right.
