# Danfoss Link for Home Assistant

Control a **Danfoss Link CC** (central controller) and everything paired to it
(Link Connect radiator thermostats, Link RS room sensors, Link FT floor thermostats,
Link HC, on/off plugs) from Home Assistant, without the Danfoss Link app.

Danfoss has discontinued the Link system and does not offer a Home Assistant
integration. This custom integration does what the phone app does: it connects through
the Danfoss cloud relay to your CC and calls the same internal REST API.

> **Unofficial.** Not affiliated with Danfoss or Trifork. It is based on reverse
> engineering and can stop working if Danfoss turns off the relay servers. You are
> changing real heating settings: use at your own risk.

## What you get

| Entity | Per | What it does |
|---|---|---|
| `climate` | room | Current and target temperature. **Auto** = follow the room's schedule, **Heat** = manual setpoint. Shows whether the room is heating. |
| `sensor` temperature | room | Room temperature for history/statistics |
| `number` at-home / away temperature | room | The two temperatures the schedule switches between |
| `select` house mode | CC | At home / Vacation / Pause (/ Boost if your CC has it) |
| `number` vacation temperature | CC | Temperature used in vacation mode |
| `switch` | on/off device | Link on/off outputs |
| `sensor` unread alarms | CC | Count plus details (low battery, lost device, ...) |

Each room is its own device (linked to the CC device), so you can put it in an area.
Polling runs every 60 s by default (configurable down to 20 s); changes are applied
immediately.

## How it works

```
Home Assistant ──mTLS──► Danfoss MDG relay (77.66.11.x / 5.179.92.x : 443)
               ══ end-to-end TLS tunnel ══► Link CC ──► http://127.0.0.1:8000/api/...
```

The CC has no local API: it closes all LAN ports and only talks to the relay. To get
through, Home Assistant presents the **identity of your paired Danfoss Link app**
(a client certificate, its private key and your CC's peer ID). That is why there is a
one-time export step below. Technical details are in [NOTICE.md](NOTICE.md) and the
docstrings in `custom_components/danfoss_link/api/`.

## Step 1: export the app identity (one time)

You need three things from an Android install of the Danfoss Link app that is
**paired with your CC**: `app_cert.pem`, `app_key.pem` and the CC peer ID. They live in
the app's private storage, so you need root access to that Android: a rooted phone or,
easier, the Android emulator on a PC/Mac.

1. **Get the app.** Install Danfoss Link from Google Play on your own phone if it is
   still listed for you, then copy the APK off it (no root needed):
   ```bash
   adb shell pm path com.danfoss.linkapp           # prints package:/data/app/.../base.apk
   adb pull /data/app/.../base.apk danfoss-link.apk
   ```
   If it is no longer available, only use an APK from a source you trust.
2. **Create an emulator** in Android Studio → Device Manager with a
   **"Google APIs"** system image (not "Google Play": Play images refuse `adb root`).
   On an Apple Silicon Mac use an **arm64-v8a** image. On an Intel/AMD PC use an
   **x86_64** image of Android 11 or newer, which can run ARM apps.
3. Start it, then `adb install danfoss-link.apk`.
4. **Pair** the app with your CC as you did on your phone. The PC must be on the same
   network as the CC. Check that your rooms show up.
5. **Export** with the script in this repository:
   ```bash
   ./scripts/export_credentials.sh ~/danfoss_link_creds
   ```
   It prints the CC peer ID (looks like `e7:af:e3:77:...`, 20 bytes) and saves the two
   PEM files. Manual alternative: the files are in
   `/data/data/com.danfoss.linkapp/files/luadata/`, and the peer ID is in the file in
   `pairings/` (`["peer_id"]="..."`) or is that file's name with `_` instead of `:`.
6. **Close the app** (and shut the emulator down). The relay allows one connection
   per identity, so the app and Home Assistant would keep kicking each other off. Don't
   remove the pairing or re-pair the app; that invalidates the exported identity.

Treat `app_key.pem` like a password: anyone with it can control your heating.

## Step 2: install the integration

**HACS:** HACS → ⋮ → Custom repositories → add `https://github.com/Ole-JH/ha-danfoss-link`
as an *Integration*, install **Danfoss Link**, restart Home Assistant.

**Manual:** copy `custom_components/danfoss_link` into your `config/custom_components/`
and restart.

## Step 3: add it

Settings → Devices & services → Add integration → **Danfoss Link**. Paste the contents
of `app_cert.pem` and `app_key.pem` and the peer ID. Instead of pasting, you can copy
the files to your config folder and enter a path such as `danfoss_link_creds/app_cert.pem`.
The form checks the key against the certificate and connects to the CC before saving.

If you re-pair the app later, use **Reconfigure** on the integration to paste the new
files. The polling interval is under **Configure**.

## Troubleshooting

| Error | Meaning |
|---|---|
| *Invalid credentials* | A PEM is incomplete, the key doesn't belong to the certificate, or the peer ID isn't 20 hex bytes. |
| *CC offline* | The relay accepted the identity but cannot reach your CC: check its internet connection and the peer ID. |
| *Cannot connect* | The relay could not be reached, or another client (the app) is using this identity. Close the app and retry. Outbound TCP 443 to `77.66.11.90/92` and `5.179.92.180/182` must be allowed. |

For more detail, enable debug logging:

```yaml
logger:
  logs:
    custom_components.danfoss_link: debug
```

Diagnostics (⋮ → Download diagnostics) redact the key, certificate, peer ID and
serial number.

## Known limitations

- **Cloud dependent.** Danfoss still ships CC firmware (4.2.1797, Feb 2025) and the
  relay works today, but there is no promise it stays up.
- **No pairing of its own yet.** Home Assistant borrows the app's identity instead of
  pairing as a separate client. Native pairing (the app's PACE/OTP flow) has not been
  reverse engineered.
- **Peer certificate not validated.** Like the app, the integration uses a custom TLS
  stack (the CC's brainpoolP320r1 certificates are unsupported by OpenSSL). It checks the
  handshake signatures but does not verify the relay's or the CC's certificate chain,
  so a man-in-the-middle on the network path is not ruled out.
- **Not yet confirmed on real hardware by this project.** The protocol client is tested
  end to end against a simulated relay and CC, and its TLS stack against OpenSSL, but
  the message formats come from reverse engineering. Please open an issue with debug
  logs if anything doesn't work with your CC.
- Schedules can't be edited from Home Assistant yet (the API supports it).

## Alternatives

If you would rather leave the cloud entirely: plain Z-Wave "Living Connect" thermostats
(014G0013 / LC-13) can be factory reset and included directly in Z-Wave JS. Link-branded
devices (014G0001/0002 thermostats, Link RS/FT/HC) use a proprietary protocol and only
work with the CC.

## Development

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The tests run the real client against `tests/fake_mdg.py`, a local fake of the relay
and CC that uses OpenSSL on the server side.
