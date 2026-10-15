# Danfoss Link protocol notes

This file records everything known about how the Danfoss Link app talks to the
Danfoss Link CC central controller, and what an integration would have to do to
emulate it. Each claim names its source. Items marked **unverified** come from
community reports nobody has confirmed against a live system.

Status as of 2026-10-15: section 0 is from decompiling the Android app; sections 1 onward are the earlier community research, kept for sources. Where they disagree, section 0 wins (for example TLS 1.2, not 1.0).

## 0. Findings from the Android app (v1.3.10)

Source: Danfoss Link Android APK 1.3.10 (`com.danfoss.linkapp`, build
2026-01-29), supplied by Ole on 2026-10-15. Java decompiled with jadx; the
bundled Lua (`luafiles/mdg-c.zip`) is Lua 5.1 bytecode in a compact variant
(header format byte `0xCC`, all ints as big-endian 7-bit varints, line info
stripped) which we converted to standard 5.1 bytecode and decompiled with
unluac. Everything below was read statically from that code; none of it has
been tested on the wire yet.

### Architecture inside the app

- Java UI (`com.danfoss.cumulus`) builds JSON requests and passes them to an
  embedded Lua VM (`libluavm-java.so`, Trifork `com.trifork.mdg.LuaVM`) over
  a pipe. The Lua "MDG" library does all networking, TLS and pairing.
- Java→Lua control commands (`R1/a.java`): `connect`, `disconnect`, `whoami`,
  `pairings`, `status`, `pair-remote otp=…`, `pair-local ip=… otp=…`,
  `place-call device_id=… protocol=… ticket=… comm_channel=…`,
  `place-call-local ip=… device_id=… protocol=…`, `revoke-pairing device_id=…`.
- The app always calls the CC with protocol name **`cc_json`**.

### Client identity (answers open question 1)

- On first start the app **generates its own key pair and a self-signed
  certificate** on curve **brainpoolP320r1** (`want_selfsigned_cert = true`,
  stored as `app_key.pem` / `app_cert.pem`). Nothing is issued by Danfoss.
- The client's **peer ID is the certificate's Subject Key Identifier**
  (`my_peer_id = cert:subject_key_identifier()`), i.e. 20 bytes. This
  explains the "20-byte peer ID" reports.
- So an integration can mint its own identity with standard tools.

### TLS to the grid (answers open question 4, on paper)

From `common_peer_config`:

```
protocol = "tlsv1_2"
curve    = "brainpoolP320r1"
cafile   = trusted-CAs.pem   -- "MDG CA", O=Trifork A/S, L=Aarhus, valid 2014-09-26..2124-04-02
verify   = { "peer", "fail_if_no_peer_cert" }
options  = { "all", "no_sslv2", "no_ticket" }
cipher   = ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-SHA256:ECDHE-ECDSA-AES128-SHA:
           ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA
```

TLS 1.2 (not 1.0 as older forum posts said), ECDSA certificates, client
certificate required. The grid's certificate must chain to the bundled
Trifork "MDG CA".

Grid endpoints, all port 443: IPv4 `77.66.11.90`, `77.66.11.92`,
`5.179.92.180`, `5.179.92.182`; IPv6 `2001:1448:248:2ea::234:18`,
`2a02:188:3f03:1ea::234:18`, `2001:1448:248:2ea::234:20`,
`2a02:188:3f03:1ea::234:20`. Control ping every 120 s. The app also sends
remote logs to `77.66.11.94:9999` (we should not).

### Grid control protocol

Protobuf 2 messages, package `mdg.control`. Handshake: `magic = 4036857000`
(`0xF09D8CA8`), `version_major = 1`, `version_minor = 0`, plus key/value
properties. Call flow: `place_call { peer_name, protocol = "cc_json" }` →
`place_call_reply { result, routing { peer_name, router[] (host, port),
token } }` → open a new connection to a router and send
`mdg.routing.Handshake { magic, version, token, supported_protocols }` →
`PleaseHold` / `GoAhead { selected_protocol }` / `RoutingError`. After
`GoAhead` the stream is end-to-end with the CC.

Remote pairing: `allocate_pairing_channel` / `place_pairing_call { pair_id }`
on the control connection, then the PACE exchange below on the routed stream.

Framing on every stream: `[u16 big-endian length][payload]`; control, routing and pairing payloads are `[u8 message tag][protobuf]`. Full client walkthrough (TLS profile, handshake properties, timers, routed TLS session, `cc_json` response envelope, LAN port 14641) is in [connection-spec.md](connection-spec.md).

**TLS quirk:** the app's bundled OpenSSL 1.0.2s is patched to advertise brainpoolP320r1 under TLS curve ID 29, which standard TLS assigns to x25519. Stock Python `ssl`/OpenSSL 3 therefore cannot talk to the grid or the CC; the client needs a small custom TLS 1.2 implementation for `ECDHE-ECDSA-*` on brainpoolP320r1.

### Pairing: PACE (answers open question 2, mostly)

`mdg_pairing_pb` + `pace.lua`: a PACE (Password Authenticated Connection
Establishment) exchange over brainpoolP320r1, using the OTP shown on the CC
as the password, with AES-256-CBC for the nonce. Messages in order:
`pace_client_hello { magic, version }` → `pace_server_hello { version, salt,
iv, z }` → `pace_xy` ↔ `pace_pk` → `pace_proof { token }` ↔ →
`contact_info { name }`. The result is that each side whitelists the other's
peer ID. The key derivation is implemented in `api/pairing.py`.

OTPs starting with `0000` are LAN pairing codes (`pair-local`); others go via
the grid (`pair-remote`).

#### Remote pairing message order (as implemented in `api/pairing.py`)

Code handling: drop `-`, ` `, `.`, `,`; the last 5 characters are the PACE
password, the rest is `pair_id` (a string, leading zeros kept).

| # | Stream | Direction | Message (tag) |
|---|---|---|---|
| 1 | control | → | `handshake` (1), `set_peer_whitelist { req_id }` (20, empty list) → reply (21) |
| 2 | control | → | `place_pairing_call { req_id, pair_id }` (32) |
| 3 | control | ← | `place_pairing_call_reply { req_id, result, routing { peer_name, router[], token } }` (33); `PEER_NOT_AVAILABLE` = wrong/expired code |
| 4 | routing | ↔ | `Handshake { token }` (0) → `PleaseHold` (1)* → `GoAhead` (2), to `router[0]` only |
| 5 | routed | ↔ | TLS 1.2, we are the client and present our new certificate |
| 6 | pairing | → | `pace_client_hello { magic = 0xF09D8CA7, 1, 0 }` (1) |
| 7 | pairing | ← | `pace_server_hello { 1, 0, salt, iv, z }` (2) |
| 8 | pairing | ↔ | `pace_xy` (3) both ways |
| 9 | pairing | ↔ | `pace_pk` (4) both ways |
| 10 | pairing | ↔ | `pace_proof { token }` (5) both ways; mismatch = wrong password |
| 11 | pairing | ↔ | `contact_info { name }` (6), each side sends first, then reads |
| 12 | control | → | `set_peer_whitelist { req_id, peer_name = [CC peer ID] }` (20) → reply (21) |

Pairing frames inside TLS are `[u16 BE length][u8 tag][protobuf]`. The CC's
peer ID is the Subject Key Identifier of the certificate it presented in step 5.

### LAN path

The MDG library supports a direct LAN connection: mDNS service type
`_danfoss-mdg._tcp.local.` (instance name = peer ID), `pair-local` and
`place-call-local`. In this app build it is **switched off**
(`enable_lan_comm = false`, `enable_mdns = false`, Java `U.a.d()` returns
false). Whether the CC itself still advertises and listens is unknown; a
quick `dns-sd -B _danfoss-mdg._tcp` on the home network would tell.

### Application protocol `cc_json` (answers open question 3)

The app sends one JSON object per message on the `cc_json` channel
(`S/i.java`):

```json
{"id": 17,
 "data": {"type": "JsonWebRequest",
          "data": {"method": "GET",
                   "uri": "api/rooms",
                   "headers": ["Accept: application/json; charset=utf-8"],
                   "content": { ... }}}}
```

Responses come back with the same `id`. It is effectively a REST API tunnelled
over the MDG channel.

Endpoints seen in the Java request classes:

| Request | Method | URI |
|---|---|---|
| All rooms | GET | `api/rooms` |
| One room | GET | `api/rooms/{id}` |
| Change room | PATCH | `api/rooms/{id}` |
| Living zone | PATCH | `api/zones/living` |
| Modes | GET / PUT | `api/modes` |
| System info / status / API versions | GET | `api/system/info`, `api/system/status`, `api/system/apiversions` |
| Temperature settings | GET / PATCH | `api/settings/temperature` |
| Alarms, on/off devices, software update, push registration | various | `api/alarms`, `api/onoffdevices`, `api/system/updatesoftware`, … |

Battery and signal (APK 1.3.10 `GetSystemInfoResponse`, `GetAlarmsResponse`,
`S/s.java`; not yet seen live):

- The CC does **not** report battery level or radio signal per thermostat.
  The app learns about them only from `GET api/alarms`.
- Alarm `titleTextId` / `descriptionTextId` are Android string resource names
  (the app looks them up with `getIdentifier`, falling back to the raw id).
  Relevant ids: `IDS_UA1_ALERTVIEWER_BATTERY_LOW_ALERT`,
  `IDS_UA1_ALERTVIEWER_BATTERY_CRITICAL_ALERT`,
  `IDS_UA1_ALERTVIEWER_NODE_DEAD_ALERT` (device no longer answers).
- Alarm `room` is read with `getString`, so it may arrive as a number or a
  string; `node` is the device within the room.
- `GET api/system/info` carries `wifiSignalStrengthDb` (int, the CC's own
  Wi-Fi), plus `uptime`, `cpuLoadPercentage`, `memoryLoadPercentage`,
  `pingCallTime`, `autoUpdateSoftware`, `limitPauseDuration`, `systemTime`.
- Strings `batteryLevel` and `signal` in the dex belong to bundled Google
  libraries, not to the CC API.

Room object (`GetRoomsResponse`), fields relevant to us:

```
id                 int
name               string
ambientTemperature number   -- current temperature (may be null)
setpoint           { type, temperature }   -- active setpoint
minSetpoint        number (default 6.0)
maxSetpoint        number (default 35.0)
regulation         { type, atHomeSetpoint, awaySetpoint, manualSetpoint }
regulationState    string
state              string ("Normal", ...)
zone               string
sortOrder          int
schedule           { monday..sunday: [ {startTimeFromMidnight, endTimeFromMidnight, state} ] }
floorSetpoint      { state: "Enabled"|..., temperature }
```

Setting a temperature (`PatchRoomRequest`), PATCH `api/rooms/{id}` with
content such as:

```json
{"id": 3, "setpoint": {"temperature": 21.5}}
{"id": 3, "regulation": {"atHomeSetpoint": 21.5}}
{"id": 3, "regulation": {"type": "Manual", "manualSetpoint": 21.5}}
```

`setpoint.temperature` changes the current period only (matches CC firmware
4.0.36 release note); `atHomeSetpoint` changes the comfort temperature.

Weekly schedules (APK 1.3.5 `PatchRoomRequest`, `S.e`, `S.f`,
`EditRoomScheduleActivity`; not yet tried live):

```
room.schedule = { monday..sunday: [ period, ... ] }      -- in GET api/rooms
period        = { startTimeFromMidnight, endTimeFromMidnight, state }
time          = .NET TimeSpan string "HH:MM:SS"; end of day is "1.00:00:00"
state         = AtHome | Away | Asleep   (rooms use AtHome/Away)
PATCH api/rooms/{id}  <- {"id": 3, "schedule": {"monday": [period, ...]}}
PATCH api/zones/living <- {"name": ..., "schedule": {...}}   (living zone)
```

- Each day is a full 00:00-24:00 cover. The editor only holds the AtHome
  periods; on save the app fills every gap with an `Away` period, sorts, and
  sends the whole day (e.g. `00:00-06:00 Away, 06:00-08:00 AtHome,
  08:00-24:00 Away`). AtHome/Away use the room's `atHomeSetpoint` /
  `awaySetpoint`, so a schedule is periods, not temperatures.
- Times snap to 15 minutes (the app rounds starts down and ends up).
- "Copy to days" sends one PATCH per weekday, each with a single day key.
  Writing a day key replaces that day.
- Living zone: rooms whose `zone` is `"Living"` share one schedule and one
  Schedule/Manual setting. The app lists them as a single "Living zone" entry
  and sends both changes to `PATCH api/zones/living` with
  `{"name": "Living", "schedule": {...}}` or
  `{"name": "Living", "regulationType": "Manual"|"Schedule"}` (note the flat
  `regulationType` key). Comfort/away setpoints stay per room on
  `api/rooms/{id}`. The app never sends `regulation.type` or `schedule` to
  `api/rooms/{id}` for a living-zone room; whether the CC accepts that is
  untested.
- The system-wide schedule (`schedule` / `normalSchedule` in
  `api/system/status`) is a flat period list for today, read-only in the app.
- On/off devices have their own schedule via `PatchOnOffDeviceRequest`
  (out of scope).

House modes (`GetModesResponse`, `PutModesRequest`, `GetSystemStatusResponse`
in APK 1.3.10):

```
GET api/modes        -> [ {type, state, startTime?, endTime?, rooms?}, ... ]
PUT api/modes/{athome|vacation|pause|boost}
                     <- {type, state, startTime?, endTime?, rooms?}
type   AtHome | Vacation | Pause | Boost   (PUT supports only these four)
state  Active | Inactive | Planned
rooms  [int]  -- sent only with AtHome + Active
GET api/system/status -> {currentState, outdoorTemperature?, schedule, normalSchedule}
currentState  AtHome | Away | Asleep | Vacation | Pause | Boost | ...
              (app keeps only the part before a comma)
```

A mode is an override. While the CC simply follows its weekly schedule every
mode is `Inactive` (inferred on Ole's CC, firmware 4.2.1724: HA showed "unknown"
while the app showed "At Home"). The "At Home" text in the app's dial is
`currentState`, i.e. the schedule's current period; `AtHome` in `api/modes`
is the separate "home now" button. The integration therefore shows
**Schedule** when no mode is active, ends an `AtHome` override with
`{"type": "AtHome", "state": "Inactive"}`, and exposes `currentState` as a
House state sensor.

### Reconstructed protobuf schemas

Reconstructed from the descriptor tables in `mdg_*_pb.lua` (field names,
numbers and types are exact; package names as in the app):

```proto
// ---- mdg_control_pb
enum mdg.control.control_message_tag {
  MT_HANDSHAKE = 1;
  MT_BYE = 2;
  MT_RECONNECT = 3;
  MT_PING = 4;
  MT_PING_REPLY = 5;
  MT_PLACE_CALL = 10;
  MT_PLACE_CALL_REPLY = 11;
  MT_INCOMING_CALL = 12;
  MT_INCOMING_CALL_REPLY = 13;
  MT_SET_PEER_WHITELIST = 20;
  MT_SET_PEER_WHITELIST_REPLY = 21;
  MT_ALLOCATE_PAIRING_CHANNEL = 30;
  MT_ALLOCATE_PAIRING_CHANNEL_REPLY = 31;
  MT_PLACE_PAIRING_CALL = 32;
  MT_PLACE_PAIRING_CALL_REPLY = 33;
}
enum mdg.control.place_call_result {
  OK = 0;
  PEER_NOT_AVAILABLE = 1;
  SERVER_ERROR = 2;
}
enum mdg.control.allocate_pairing_channel_result {
  APC_OK = 0;
  APC_DENIED = 1;
  APC_SERVER_ERROR = 2;
}
message mdg.control.endpoint {
  required bytes host = 1;
  required uint32 port = 2;
}
message mdg.control.routing_info {
  required string peer_name = 3;
  repeated .mdg.control.endpoint router = 4;
  required bytes token = 5;
}
message mdg.control.property {
  required string key = 1;
  required string value = 2;
}
message mdg.control.handshake {
  required fixed32 magic = 1;
  required uint32 version_major = 2;
  required uint32 version_minor = 3;
  repeated .mdg.control.property property = 4;
}
enum mdg.control.bye.bye_reason {
  NO_ERROR = 0;
  INTERNAL_ERROR = 1;
  SYNTAX_ERROR = 2;
  DENIED = 3;
  TEMP_DENIED = 4;
  PROTOCOL_ERROR = 5;
  TIMEOUT = 6;
  UNSUPPORTED_VERSION = 7;
}
message mdg.control.bye {
  required .mdg.control.bye.bye_reason reason = 1;
  optional string reason_text = 2;
}
message mdg.control.ping {
  required uint32 req_id = 1;
  optional uint32 perceived_qos = 2;
}
message mdg.control.ping_reply {
  required uint32 req_id = 1;
}
message mdg.control.reconnect {
  optional .mdg.control.endpoint new_host = 1;
  optional uint32 suggested_delay = 2;
}
message mdg.control.place_call {
  required uint32 req_id = 1;
  required string peer_name = 2;
  optional string protocol = 3;
  optional string ticket = 4;
}
message mdg.control.place_call_reply {
  required uint32 req_id = 1;
  required .mdg.control.place_call_result result = 2;
  optional .mdg.control.routing_info routing = 3;
  optional string result_text = 4;
}
message mdg.control.incoming_call {
  required uint32 req_id = 1;
  required .mdg.control.routing_info routing = 2;
  optional string protocol = 3;
  optional string ticket = 4;
}
message mdg.control.incoming_call_reply {
  required uint32 req_id = 1;
  required bool will_accept_call = 2;
}
message mdg.control.set_peer_whitelist {
  required uint32 req_id = 1;
  repeated string peer_name = 2;
}
message mdg.control.set_peer_whitelist_reply {
  required uint32 req_id = 1;
}
message mdg.control.allocate_pairing_channel {
  required uint32 req_id = 1;
}
message mdg.control.allocate_pairing_channel_reply {
  required uint32 req_id = 1;
  required .mdg.control.allocate_pairing_channel_result result = 2;
  optional string pair_id = 3;
  optional uint64 end_of_validity = 4;
}
message mdg.control.place_pairing_call {
  required uint32 req_id = 1;
  required string pair_id = 2;
}
message mdg.control.place_pairing_call_reply {
  required uint32 req_id = 1;
  required .mdg.control.place_call_result result = 2;
  optional .mdg.control.routing_info routing = 3;
  optional string result_text = 4;
}
message mdg.control.incoming_pairing_call {
  required uint32 req_id = 1;
  required .mdg.control.routing_info routing = 2;
  required string pair_id = 3;
}
message mdg.control.incoming_pairing_call_reply {
  required uint32 req_id = 1;
  required bool will_accept_call = 2;
}
// ---- mdg_library_pb
enum mdg.library.control_message_tag {
  MT_REQUEST = 1;
  MT_POS_RESPONSE = 2;
  MT_NEG_RESPONSE = 3;
  MT_NOTIFICATION = 4;
}
message mdg.library.Property {
  required string key = 1;
  required string value = 2;
}
message mdg.library.Request {
  required uint32 request_id = 1;
  required string operation = 2;
  repeated .mdg.library.Property parameters = 3;
}
message mdg.library.PosResponse {
  required uint32 request_id = 1;
  required string response = 2;
}
message mdg.library.NegResponse {
  required uint32 request_id = 1;
  required string error_text = 2;
}
message mdg.library.Notification {
  required string event = 1;
  repeated .mdg.library.Property parameters = 2;
}
// ---- mdg_pairing_pb
enum pairing_message_tag {
  MT_PACE_CLIENT_HELLO = 1;
  MT_PACE_SERVER_HELLO = 2;
  MT_PACE_XY = 3;
  MT_PACE_PK = 4;
  MT_PACE_PROOF = 5;
  MT_CONTACT_INFO = 6;
}
message pace_client_hello {
  required fixed32 magic = 1;
  required uint32 version_major = 2;
  required uint32 version_minor = 3;
}
message pace_server_hello {
  required uint32 version_major = 1;
  required uint32 version_minor = 2;
  required bytes salt = 3;
  required bytes iv = 4;
  required bytes z = 5;
}
message pace_xy {
  required bytes xy = 1;
}
message pace_pk {
  required bytes pk = 1;
}
message pace_proof {
  required bytes token = 1;
}
message contact_info {
  optional string name = 1;
}
// ---- mdg_routing_pb
enum mdg.routing.routing_message_tag {
  RMT_HANDSHAKE = 0;
  RMT_PLEASEHOLD = 1;
  RMT_GOAHEAD = 2;
  RMT_ROUTINGERROR = 3;
}
message mdg.routing.Handshake {
  required fixed32 magic = 1;
  required uint32 version_major = 2;
  required uint32 version_minor = 3;
  required bytes token = 4;
  repeated string supported_protocols = 5;
}
message mdg.routing.PleaseHold {
}
message mdg.routing.GoAhead {
  optional string selected_protocol = 1;
}
enum mdg.routing.RoutingError.RoutingErrorTypes {
  NO_ERROR = 0;
  SERVER_ERROR = 1;
  SYNTAX_ERROR = 2;
  INVALID_TOKEN = 3;
  PEER_TIMEOUT = 4;
  UNSUPPORTED_VERSION = 5;
}
message mdg.routing.RoutingError {
  required .mdg.routing.RoutingError.RoutingErrorTypes error = 1;
}
```

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
