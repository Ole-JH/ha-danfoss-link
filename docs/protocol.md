# Danfoss Link protocol notes

This file records everything known about how the Danfoss Link app talks to the
Danfoss Link CC central controller, and what an integration would have to do to
emulate it. Each claim names its source. Items marked **unverified** come from
community reports nobody has confirmed against a live system.

Status as of 2026-10-15: research only, no captures from our own system yet.

## 1. System overview

```
Danfoss Link app ──TLS──▶ Trifork SecureDeviceGrid ("MDG", cloud relay) ◀──TLS── Link CC ──Z-Wave (proprietary)──▶ Living Connect / RS / FT
```

- The Link CC is a Z-Wave controller. Thermostats (Living Connect 014G0002 on
  firmware 3.x/4.x, Link RS, Link FT) talk to it over a proprietary Danfoss
  command class, so a standard Z-Wave stick cannot control them. Only the old
  014G0013 / firmware 2.06 units speak standard Z-Wave. [openHAB thread #6,
  #97; vindvejr review; Danfoss technical paper]
- App connectivity arrived with the Link CC **Mk IV** (built-in Wi-Fi, firmware
  4.0.12, 2015-07-01). Earlier Mk II/Mk III units have no network at all.
  [Danfoss technical paper AM193786404979en]
- Every app session goes through the cloud relay, also on the same LAN. The CC
  exposes no listening ports in normal operation. [vindvejr review 2017;
  openHAB thread, Sonic #111] The Danfoss support article lists only ports
  80 and 443 as needed for the CC. [linkapp.danfoss.com support article]

## 2. Transport: SecureDeviceGrid (SDG / "MDG")

The relay is Trifork's SecureDeviceGrid, also used by DEVIreg Smart and
Danfoss Icon. The Link CC uses an **older variant** of the protocol than those
products.

### Grid servers

| Host | Port | Source |
|---|---|---|
| `mdg-danfoss.hosted.trifork.com` | 443 | openHAB thread, ChristianHC #18 |
| `77.66.11.90`, `77.66.11.92`, `77.66.11.94` | 443 | openHAB #18/#27; opensdg `grid.c` |
| `5.179.92.180`, `5.179.92.182` | 443 | openHAB #18/#27; opensdg `grid.c` |

On 2026-10-15 `mdg-danfoss.hosted.trifork.com` still resolves, to eight
addresses: `77.66.11.90`, `77.66.11.92`, `77.66.7.41`, `77.66.7.42`,
`5.179.92.171`, `5.179.92.172`, `5.179.92.180`, `5.179.92.182`. The TLS
parameters could not be probed from our sandbox (its egress proxy terminates
TLS), so question 4 below is still open.

The app has the IPs hard-coded with the hostname as fallback, so a DNS
redirect alone does not intercept it. [ChristianHC #23]

### Two generations of the tunnel

| | Legacy (Link CC) | Current (DEVIreg Smart, Icon) |
|---|---|---|
| Encryption | Standard TLS, brainpool EC certificates (app ships `brainpoolP320r1-curve.pem`), client certificate | CurveCP-style handshake on libsodium (Curve25519 + XSalsa20-Poly1305) |
| Peer ID | Short ID (reported as 20 bytes, later as a long ASCII string; **conflicting**) | 32-byte Curve25519 public key, hex |
| Open implementation | None public | `Sonic-Amiga/opensdg` (C, GPL-3.0), `opensdg-java` |

Sources: scaarup #16 (brainpool TLS on 443, `openssl` "EC curve issue");
ChristianHC #18 (`openssl s_client -tls1` with client cert/key and
`trusted-CAs.pem`); Sonic #106, #108, #132, #152. The grid servers accept both
variants on the same port and autodetect; a device speaks only one. [Sonic #132]

TLS 1.0 plus brainpool curves is a practical problem: modern OpenSSL builds and
Python's `ssl` module may refuse it by default. This needs testing.

### Connection flow (both generations)

From Sonic #111/#132 and opensdg `peer.c`:

1. Client connects to a grid server and completes the tunnel handshake.
2. Client sends a "call remote" request naming the target peer ID and an
   application protocol string. (`ConnectToPeer { id, peerId, protocol }`)
3. Grid replies with `PeerReply { id, result (0 = OK), PeerInfo { peerId,
   ForwardHost { host, port }, tunnelId } }`.
4. Client opens a second connection to `host:port` and sends
   `ForwardRemote { magic, protocolMajor, protocolMinor, tunnelId, signature }`
   (signature `"Mdg-NaCl/binary"` in the current generation).
5. Grid asks the device, then forwards. The client now runs a second,
   end-to-end handshake with the device itself; the grid only relays bytes.
6. Keep-alive pings every 30 s or the grid drops the connection.

Grid control messages are protobuf 2 (`control_protocol.proto` in opensdg:
`ConnectToPeer`, `PeerReply`, `PeerInfo`, `ForwardRemote`, `ForwardReply`,
`ForwardError`, `PairRemote`, `Ping`/`Pong`, `IncomingCall`). Whether the
legacy generation uses the same messages inside TLS is **unverified**.

Current-generation framing, for reference (opensdg `tunnel_protocol.h`):
2-byte BE length, then `{ u16 size; u32 magic = F0 9F 90 9F; u32 command }`,
commands `TELL`, `WELC`, `HELO`, `COOK`, `VOCH`, `REDY`, `MESG`. `MESG`
payload is `[u16 BE size][u8 dataType][protobuf]`.

## 3. Discovery

There is no LAN discovery. The app finds the CC by its peer ID, which it
learns during pairing and stores. The grid resolves the peer ID to a relay
route. [Sonic #111]

During first-time Wi-Fi setup the CC runs as an access point and accepts a
plain (unencrypted) mdglib socket on its private network; it stops listening
once Wi-Fi is configured. [Sonic #111] Port not documented.

## 4. Pairing and auth

- The CC shows a one-time pairing code (OTP) in its menu; the app enters it.
- Pairing is a challenge-response through the grid; no secret leaves either
  end in clear. The result is that the app learns the CC's peer ID and the CC
  adds the app to its whitelist. "Share house" pairs further phones the same
  way. [Sonic #111]
- In the current generation (opensdg `peer.c`), only the OTP minus its last 3
  digits goes to the grid (`PairRemote { id, otp }`); the full OTP feeds a
  SHA-512 / XSalsa20 / Curve25519 challenge (`PAIRING_CHALLENGE`,
  `PAIRING_RESPONSE`, `PAIRING_RESULT`). The legacy Link CC pairing messages
  are **not documented anywhere public**.
- In the legacy generation the app authenticates with a TLS client
  certificate. Whether that certificate is generated per install, shipped in
  the APK, or issued by the grid during pairing is **unknown**, and it is the
  key question for emulation.

## 5. Application layer (app ↔ CC)

- Protobuf 2.6 messages carried in the end-to-end tunnel. [ChristianHC #32]
- The app addresses resources with REST-like paths. Paths seen in the
  decompiled Android app [ChristianHC #27]:

```
/api/system/apiversions   /api/system/status   /api/system/info
/api/system/updatesoftware
/api/rooms                /api/zones/living
/api/modes                /api/alarms          /api/onoffdevices
/api/settings/temperature
```

- For our scope, `/api/rooms` (room temperature and setpoint) and
  `/api/modes` are the likely endpoints. Request/response schemas, field
  numbers and the method encoding (GET vs PUT) are **not published**.
  ChristianHC reverse-engineered the `.proto` files and reported full access to
  his own CC in Oct 2018 (#43), but his Bitbucket repo
  (`bitbucket.org/ChristianHC/danfoss-link`) is private.
- The app bundles `libmdg.so` and `mdg-c` Lua sources (possibly obfuscated
  bytecode). `libmdg.so` links against Android bionic and does not run on
  plain Linux. [ChristianHC #18, #50, #66; GBilic #145]

Android package: `com.danfoss.link` (**unverified**). Last app release noted
by Danfoss: Android 1.3.5 (2020-06-15), iOS 1.3.2 (2023-10-18).

## 6. Z-Wave side (for reference)

Sniffed with a Suphacap stick and with RTL-SDR (`rtl-zwave`, `waving-z`):
thermostat→CC frames on command class 145 (0x91, manufacturer proprietary)
carry current temperature and setpoint as centi-degrees, e.g.
`...0930...` = 23.52 °C. [scaarup #16, #126] Read-only; nobody has
injected setpoints this way.

## 7. Other routes people use

| Route | Read temp | Set setpoint | Notes |
|---|---|---|---|
| Alexa skill → `alexa-remote2` in Node-RED (`michaelflarsen/danfosslink-in-HA`, MIT, active 2026) | Yes, every 5 min | Claimed | Needs Amazon account + Danfoss Alexa skill (CC fw ≥ 4.2.1475) |
| Alexa → Homebridge → HomeKit | Yes | No (`State not available`) | HA forum, 2024 |
| Alexa via bespoken.io (`JonasPed/danfosslink2mqtt`) | Yes | Yes | Archived 2021, paid service |
| Z-Wave sniffing (`scaarup/hass-danfoss`) | Yes | No | Needs SDR or sniffer stick |

## 8. Open questions

1. How does the legacy app obtain its TLS client certificate and peer ID?
2. Exact legacy grid handshake and pairing messages.
3. `.proto` schemas for `/api/rooms` read and setpoint write.
4. Does the grid still accept legacy TLS 1.0 / brainpool today?

Questions 1 to 3 are answerable by decompiling the Android APK (Java/Kotlin
plus the bundled Lua and `.proto` descriptors). Question 4 is answerable by a
TLS probe against the grid IPs.

## Sources

- openHAB community, "Danfoss living connect, new proprietary z-wave binding",
  https://community.openhab.org/t/danfoss-living-connect-new-proprietary-z-wave-binding/34263
  (post numbers cited as #N)
- Sonic-Amiga/opensdg, https://github.com/Sonic-Amiga/opensdg (GPL-3.0)
- Sonic-Amiga/opensdg-java, https://github.com/Sonic-Amiga/opensdg-java
- Danfoss, "Overview - Danfoss Link and Connect software versions",
  https://assets.danfoss.com/documents/latest/468982/AM193786404979en-001101.pdf
- Danfoss support, "I have no connection to Danfoss Link CC",
  https://linkapp.danfoss.com/en/articles/index?id=ka03b000000cFnUAAU
- Jacob Laursen, Danfoss Living Connect review, https://techblog.vindvejr.dk/?p=166
- Home Assistant community, "Danfoss Link compatibility?",
  https://community.home-assistant.io/t/danfoss-link-compatibility/139248
- https://github.com/michaelflarsen/danfosslink-in-HA
- https://github.com/JonasPed/danfosslink2mqtt
