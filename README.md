<p align="center">
  <img src="docs/images/icon.png" alt="" width="128" height="128">
</p>

# Danfoss Link for Home Assistant

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Ole-JH&repository=ha-danfoss-link&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=danfoss_link)

Control a **Danfoss Link CC** (central controller) and everything paired to it
(Link Connect radiator thermostats, Link RS room sensors, Link FT floor thermostats,
Link HC, on/off plugs) from Home Assistant, without the Danfoss Link app.

Danfoss has discontinued the Link system and does not offer a Home Assistant
integration. This custom integration does what the phone app does: it connects through
the Danfoss cloud relay to your CC and calls the same internal REST API. It has been
confirmed working against a real Link CC.

> **Unofficial.** Not affiliated with Danfoss or Trifork. It is based on reverse
> engineering and can stop working if Danfoss turns off the relay servers. You are
> changing real heating settings: use at your own risk.

## Quick start

You need Home Assistant 2025.7 or newer with [HACS](https://hacs.xyz), and a Link CC
that is online. You don't need your phone or the Danfoss Link app.

1. **Add the repository to HACS.** Click the first blue button above (or in HACS go to
   ⋮ → Custom repositories and add `https://github.com/Ole-JH/ha-danfoss-link` as an
   *Integration*).
2. **Download** Danfoss Link in HACS, then **restart Home Assistant**.
3. **Add the integration.** Click the second blue button above (or Settings → Devices &
   services → Add integration → **Danfoss Link**) and choose
   **Pair with the code from the CC**.
4. **Get a code from the CC.** On the CC's touch screen, start pairing a new app, the
   same way you once paired your phone. The CC shows a pairing code. Codes expire
   quickly, so have Home Assistant open and ready.
5. **Enter the code** in Home Assistant (dashes and spaces don't matter) and submit.
   Home Assistant pairs, connects through the Danfoss cloud and reads your rooms before
   it saves anything.

That's it. If a step fails, see [Troubleshooting](docs/troubleshooting.md). If pairing
doesn't work for you at all, you can [use exported app credentials](docs/exported-credentials.md)
instead.

**Manual install:** copy `custom_components/danfoss_link` into your
`config/custom_components/` and restart Home Assistant.

## What you get

Each room on the CC becomes its own device (linked to the CC device), so you can put
it in an area.

| Entity | Per | What it does |
|---|---|---|
| `climate` | room | Current and target temperature. **Auto** = Schedule, **Heat** = Manual (see [Modes](docs/features.md#modes)). Shows whether the room is heating. |
| `sensor` temperature | room | Room temperature for history/statistics |
| `select` mode | room | **Schedule** / **Manual**, the same setting as the thermostat's Auto/Heat, for dashboards and automations |
| `number` at-home / away temperature | room | The two temperatures the schedule switches between |
| `select` house mode | CC | Schedule (no override) / At home / Vacation / Pause (/ Boost if your CC has it) |
| `sensor` house state | CC | What the CC is doing now: at home / away / asleep from the schedule, or the active mode |
| `number` vacation temperature | CC | Temperature used in vacation mode |
| `sensor` pause end | CC | When a running pause ends; unknown when there is no pause or it runs until you resume |
| `sensor` vacation start / end | CC | When a planned or running vacation starts and ends; unknown when none is planned |
| `switch` | on/off device | Link on/off outputs; unavailable while the CC reports "no response from unit" for them |
| `sensor` unread alarms | CC | Count plus details (low battery, lost device, ...) |
| `button` mark alarms as read | CC | Marks every unread alarm as read, as opening it in the app does |
| `binary_sensor` living zone | room | On when the room is in the living zone. Its rooms share one Schedule/Manual mode and one schedule, so changing one changes all. |
| `binary_sensor` battery | room | Low battery on a device in the room, from the CC's alarms. *Disabled by default.* |
| `binary_sensor` radio connection | room | Off when the CC has lost contact with a device in the room. *Disabled by default.* |
| `sensor` Wi-Fi signal | CC | The CC's Wi-Fi signal strength. *Disabled by default.* Only created if the CC reports it; firmware 4.2.1797 does not. |

Entities marked *disabled by default* can be turned on in the device's entity list.
Home Assistant reads the CC every 60 seconds (configurable down to 20) and sends
changes right away. Your phone app keeps working alongside it.

Actions for planned vacations, pausing and weekly schedules, the bundled schedule and
pause dashboard cards, the feature comparison with the app and how the modes behave
are in [docs/features.md](docs/features.md).

## How it works

```
Home Assistant ──mTLS──► Danfoss MDG relay (77.66.11.x / 5.179.92.x : 443)
               ══ end-to-end TLS tunnel ══► Link CC ──► http://127.0.0.1:8000/api/...
```

The CC has no local API: it closes all its LAN ports and only talks to the Danfoss
cloud relay. Home Assistant pairs with the CC as its own app and goes through the same
relay as the phone app, so the integration needs the CC and Home Assistant to be online.
More in [docs/how-it-works.md](docs/how-it-works.md).

## Known limitations

- **Cloud dependent.** The relay works today, but Danfoss makes no promise it stays up.
  If it is down, the entities become unavailable and the CC keeps heating on its own
  schedule.
- **Cloud pairing only.** Local-network pairing codes (starting with `0000`) are not
  supported.
- **Tested on one setup so far.** Message formats come from reverse engineering, so
  other device mixes may behave differently. Please open an issue with debug logs if
  something doesn't work with your CC.

## Common problems

- *No CC is waiting for this code*: the code expired or was mistyped. Start pairing on
  the CC again and enter the new code quickly.
- *CC offline*: the relay can't reach your CC. Check the CC's internet connection.
- *Cannot connect*: the relay is unreachable, or another client is using the same
  identity. If you use exported app credentials, close the Danfoss Link app. Outbound
  TCP 443 to the relay must be allowed.

Everything else, including debug logging, is in
[docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

- [Entities and features](docs/features.md): every entity, planned vacations, weekly schedules, feature
  coverage compared with the app, and what to expect after setup.
- [Troubleshooting](docs/troubleshooting.md): error messages, debug logging and
  diagnostics.
- [Use exported app credentials](docs/exported-credentials.md): the alternative to
  pairing, using the identity of a paired Android app.
- [How it works](docs/how-it-works.md): the cloud relay, pairing details, known
  limitations and cloud-free alternatives.
- [Development](docs/development.md): running the tests and linters.
- Protocol notes: [protocol.md](docs/protocol.md) and
  [connection-spec.md](docs/connection-spec.md).
