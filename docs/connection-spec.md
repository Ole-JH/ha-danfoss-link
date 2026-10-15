# Danfoss Link CC – client connection spec (MDG "grid" + cc_json)

Reconstructed statically from the Danfoss Link Android app 1.3.10 (Lua 5.1 "lush" runtime + Java).
Sources are cited as `file:function/line`; `dec/` = `apk/x/luafiles/dec/`, `jadx/` = `apk/jadx/sources/`.
Confidence tags: **[confirmed]** read directly in code, **[inferred]** strong inference, **[UNCERTAIN]** needs a live test.

---

## 0. Big picture

```
 HA client                    MDG control server (Trifork)          MDG router             CC (Danfoss Link)
   |-- TCP :443 ---------------->|                                       |                        |
   |== TLS1.2 (client cert) =====|                                       |                        |
   |-- [len][0x01]handshake ---->|                                       |                        |
   |-- [len][0x14]set_whitelist->|   (CC has its own control conn too)   |                        |
   |-- [len][0x04]ping --------->|                                       |                        |
   |-- [len][0x0a]place_call(peer_name=<CC id>, protocol="cc_json") ---->|                        |
   |<- [len][0x0b]place_call_reply(OK, routing{peer_name,router[],token})|                        |
   |                              |-- incoming_call(routing,"cc_json") ---------------------------->|
   |-- plain TCP to router host:port ------------------------------------>|<----- CC connects -----|
   |-- [len][0x00]routing.Handshake(magic,token,["tlsv1_2"]) ------------>|                        |
   |<- [len][0x01]PleaseHold (optional) / [len][0x02]GoAhead -------------|                        |
   |== end-to-end TLS1.2 inside routed TCP; HA = TLS client, CC = TLS server ====================>|
   |-- [len]{"id":1,"data":{"type":"JsonWebRequest","data":{...}}} ----------------------------->|
   |<- [len]{"id":1,"cc_time":12,"data":{"type":"JsonWebResponse","data":{"status":200,...}}} ----|
```

`[len]` = u16 big-endian length of the following payload, always (lush `cotcp` option `packet=2`, header format
`struct ">I2"`; `dec/mdg_cotcp.lua` setopt handler ~l.960, send ~l.1044, recv ~l.627). **[confirmed]**

There is **no multiplexer / channel layer on the wire.** `mdg_multiplexer*` and the Java `R1/d.java` channel
code (open/ack/close meta packets, channel parity) only bridge the Java UI and the Lua VM over local pipes
(`android_main`: `channels_read_fd = lush_in`). On the wire, one u16 frame inside the peer TLS session = one
complete JSON message (`dec/mdg_mdg.lua` `routing_proc_loop`: every `tcp` packet -> `mux:send(data)`, every
`mux_data` -> `cotcp.send(sock, data)`). **[confirmed]**

An alternative **LAN path** (no cloud) exists and is enabled on the CC; see §6.

---

## 1. TLS profile (applies to ALL three TLS sessions)

From `dec/mdg_common_peer_config.lua` (`ssl_params`) + `dec/mdg_android_main.lua`:

| setting | value |
|---|---|
| protocol | TLS 1.2 only (`tlsv1_2`) |
| ciphers (order) | `ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-SHA256:ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA` |
| curve | `brainpoolP320r1` (ECDHE and all ECDSA keys) |
| options | `all, no_sslv2, no_ticket` (do not send SessionTicket ext) |
| verify | `{"peer","fail_if_no_peer_cert"}`, `cafile = trusted-CAs.pem` (single cert: `C=DK, L=Aarhus, O=Trifork A/S, CN=MDG CA`, 320-bit EC key, ecdsa-with-SHA256, valid to 2124) |
| client cert/key | `$LUSH_DATA_DIR/app_cert.pem` / `app_key.pem` (self-signed, generated on first start: `want_selfsigned_cert=true`, `ecgroup.generate_certificate` with `brainpoolP320r1-curve.pem`) |
| SNI | **none** – `cotcp.ssl_upgrade` does `ssl.wrap` + `dohandshake` with no `sni()` call (`dec/mdg_cotcp.lua` ~l.883-930) **[confirmed]** |
| hostname check | **none** (LuaSec never checks hostnames; control hosts are raw IPs) **[confirmed]** |

Post-handshake checks the app performs (control: `mdg_mdg_control_connection.lua` L20_1; routed: `mdg_mdg_routing_connection.lua` L19_1):
`protocol == "TLSv1.2"`, key exchange `ECDH`/`ECDHE`, authentication `ECDSA`, cipher bits `>= 256`.
(Note: "bits >= 256" as reported by LuaSec `getpeerconninfo`; AES128 suites evidently pass in practice, so this is
probably the EC key/curve size, not the cipher strength. **[UNCERTAIN]** – irrelevant for a client that just negotiates.)

### 1.1 CRITICAL: non-standard curve ID 29 = brainpoolP320r1

The bundled OpenSSL **1.0.2s is patched** (`apk/x/lib/arm64-v8a/libluavm-java.so`, statically disassembled, not executed):

* `tls1_ec_nid2curve_id` has an extra case `NID 929 (brainpoolP320r1) -> 29` (instr. at `0x13f400`).
* `nid_list[28] = 929` (table at `0x1ebb30`), so curve id 29 decodes to brainpoolP320r1.
* default `supported_groups`/`elliptic_curves` list (bytes at `0x1ebae0`): `0x0017, 0x001d, 0x0019, 0x001c, 0x001b, 0x0018, 0x001a, 0x0016, 0x000e, 0x000d, 0x000b, 0x000c, 0x0009, 0x000a`.

So in this ecosystem **named-curve id `0x001d` (29) means brainpoolP320r1**, colliding with IANA's x25519.
All certificates (MDG CA, very likely the MDG server, every app and CC self-signed cert) are brainpoolP320r1
ECDSA keys, and ECDHE uses brainpoolP320r1 (CC config `curve=brainpoolP320r1` = fixed tmp-ECDH curve).
**[confirmed for the client library; server side inferred]**

Consequence for Python: stock `ssl`/OpenSSL 3 cannot complete these handshakes (it would treat 29 as x25519,
and rejects server certs whose curve has no TLS group id). A minimal client needs its own TLS 1.2 client for
`ECDHE-ECDSA-AES128-GCM-SHA256` (and ideally `-AES128-SHA256`/`-AES128-SHA` fallbacks):

* ClientHello: version 0x0303, the cipher list above, extensions: `supported_groups` = `[0x001d]` (optionally
  followed by 0x0017), `ec_point_formats` = `[0]` (uncompressed), `signature_algorithms` (include 0x0403 ECDSA+SHA256,
  0x0503, 0x0603, 0x0203). No SNI, no session ticket. No ALPN.
* ServerKeyExchange: `curve_type=3 (named_curve)`, `namedcurve=0x001d`, point = 81 bytes (`04 || X(40) || Y(40)`),
  signed with the server's brainpoolP320r1 ECDSA key.
* CertificateRequest will come (both MDG server and CC require a client cert). Send own cert + CertificateVerify
  (ECDSA/SHA-256 over handshake transcript) with the brainpoolP320r1 key.
* ClientKeyExchange: own ephemeral brainpoolP320r1 point (81 bytes uncompressed). Pre-master = 40-byte X coordinate.
* PRF: TLS 1.2 P_SHA256 (P_SHA384 for the *SHA384 suites). Records: standard AES-GCM (explicit 8-byte nonce).

Practical options: (a) `tlslite-ng` (pure Python, uses `python-ecdsa`, which ships `BRAINPOOLP320r1`) patched to map
group 29 -> `ecdsa.curves.BRAINPOOLP320r1` and to accept a bp320r1 cert; (b) a small hand-written TLS 1.2 client
using `python-ecdsa` for ECDH/ECDSA and `cryptography` for AES-GCM/HMAC (note `cryptography` itself has **no**
brainpoolP320r1). Both are a few hundred lines; the handshake happens rarely.

---

## 2. Control connection (to MDG control server)

### 2.1 Endpoints & connect strategy **[confirmed]** (`mdg_common_peer_config.lua`, `mdg_mdg_control_sup.lua`)
* `mdg_control_hosts_hardcoded_ip_sets` (used because non-empty), one set chosen per attempt (shuffled, round robin):
  * set A: `[2001:1448:248:2ea::234:18]:443`, `[2a02:188:3f03:1ea::234:18]:443`, `77.66.11.90:443`, `5.179.92.180:443`
  * set B: `[2001:1448:248:2ea::234:20]:443`, `[2a02:188:3f03:1ea::234:20]:443`, `77.66.11.92:443`, `5.179.92.182:443`
* All hosts of a set are TCP-connected **in parallel**; first success wins, others closed ~2 s later. No DNS name is used.
* Then TLS (client mode, §1). Server cert must chain to the Trifork MDG CA (strict; no `lsec_continue` here).

### 2.2 Messages: `[u16 len][u8 tag][protobuf]` (proto in `mdg.proto`, package `mdg.control`)
Tags: HANDSHAKE=1, BYE=2, RECONNECT=3, PING=4, PING_REPLY=5, PLACE_CALL=10, PLACE_CALL_REPLY=11, INCOMING_CALL=12,
INCOMING_CALL_REPLY=13, SET_PEER_WHITELIST=20, SET_PEER_WHITELIST_REPLY=21, ALLOCATE_PAIRING_CHANNEL=30/31, PLACE_PAIRING_CALL=32/33.

### 2.3 Sequence sent by the app right after TLS is up **[confirmed]**
1. **handshake** (tag 1) – sent immediately, without waiting for the server (`L22_1`):
   `magic = 4036857000 (0xF09D8CA8, fixed32 => bytes A8 8C 9D F0)`, `version_major = 1`, `version_minor = 0`,
   `property[] = {key,value}` (order = Lua `pairs`, i.e. arbitrary). Properties sent by the app:
   * from Java `S/n.java y()` (forwarded via Lua API `connect` with `p_` prefix stripped):
     `app.package.name=com.danfoss.linkapp`, `app.display.name=release`, `app.version.name=Android-1.3.10`,
     `app.version.code=11565`, `app.git.sha=022352f5151e00e3b2ed95fa71e4b8e1aca6fd4e`, `app.build.time=2026-01-29T12:31Z`,
     `app.device.os=Android API <n>`, `app.device.type=<Brand Model>`, `app.device.battery=<float>`,
     `app.cpu.info=<...>`, `app.network.type=<...>`
   * added by `mdg_control_sup` L23_1: `mdg.connect.count=<n>`, `mdg.git.sha=b23dd22487209badd6167b137aeb00d636ae9f73`,
     `mdg.process.start.time=<ts>`, `mdg.process.time.now=<ts>`; ts format `"%d-%m-%Y_%X.mmm"` e.g. `15-10-2026_14:03:07.123`.
   Properties look informational (logging); sending the `mdg.*` + a few `app.*` keys is recommended. **[UNCERTAIN whether server requires any]**
   Minimal example without properties: `00 0a 01 0d a8 9a 9f f0 10 01 18 00`.
2. **set_peer_whitelist** (tag 20) `req_id=1`, `peer_name[] = ` all paired CC ids (from pairings DB). Reply tag 21 `{req_id}`.
3. **ping** (tag 4) `req_id=2` (sup's liveness check; `perceived_qos` never set). Reply tag 5 `{req_id}`; 5 s timeout, else the app tears down.
* `req_id`: single counter per connection starting at 1, incremented for every request (whitelist, ping, place_call, ...).
* Server's own HANDSHAKE (tag 1) is received and **ignored**. No server field is checked. **[confirmed]**

### 2.4 Keepalive / timers **[confirmed]**
* Ping interval = `0.9 * control_ping_interval` = `0.9 * 120 = 108 s` measured from the **last message sent** on the control
  connection (every send resets the pinger). "Aggressive" mode (10 s) only when the UI requests it.
* Ping reply must arrive within 5 s, else disconnect + reconnect.
* If nothing at all is **received** for 1000 s -> error "It is quiet - too quiet", reconnect.
* The client loop does not handle an incoming MT_PING (it would error out) – server evidently never pings. A robust
  client should still answer tag 4 with tag 5 `{req_id}`.

### 2.5 RECONNECT / BYE **[confirmed]**
* RECONNECT (tag 3) `{new_host: endpoint{host,port}, suggested_delay}`: close, sleep `max(0, min(2*mdg_control_max_wait, suggested_delay))`
  (Android: `max_wait=1` → ≤2 s), reconnect to that single host:port.
* BYE (tag 2) `{reason, reason_text}`: log, close, reconnect with backoff (Android `min_wait=0`, `max_wait=1`; formula
  `mdg_control_sup` L19_1: after a short-lived connection `max(min_wait, min(max_wait, prev*2))`, after a long-lived one
  random `min_wait*rand` ... – just use 1–60 s exponential backoff).
  Reasons: 0 NO_ERROR,1 INTERNAL_ERROR,2 SYNTAX_ERROR,3 DENIED,4 TEMP_DENIED,5 PROTOCOL_ERROR,6 TIMEOUT,7 UNSUPPORTED_VERSION.
* `endpoint.host` is `bytes` but is passed unchanged to `socket.connect(host, port)` → it is an **ASCII hostname/IP text**,
  not packed binary. **[inferred]** (defensive: if 4/16 non-printable bytes, treat as packed IPv4/IPv6).

### 2.6 place_call **[confirmed]** (`mdg_mdg_control_protocol.lua create_place_call`, `mdg_mdg_comm_strategy.lua`)
```
place_call (tag 10) { req_id = next, peer_name = <CC peer id>, protocol = "cc_json", [ticket omitted] }
```
* `peer_name` = the CC's **peer id** = the `device_id` stored at pairing = LuaSec `x509:subject_key_identifier()` of the
  CC certificate. Format (disassembled `meth_subject_key_identifier` @0xa5d80, hex table "0123456789abcdef"):
  **lowercase hex, bytes separated by ':'**, of the 20-byte SKI, e.g. `3f:a1:...:9c` (59 chars).
  Same format is used for the app's own id and for whitelist entries. Pairing files on the phone are named with
  ':' replaced by '_' (`$LUSH_DATA_DIR/pairings/<id_with_underscores>`).
* `ticket`: Java passes `null` → dropped → field absent. **[confirmed for remote place-call]**
* Reply tag 11 within 9 s: `{req_id, result: 0 OK | 1 PEER_NOT_AVAILABLE | 2 SERVER_ERROR, routing, result_text}`.
  On OK use `routing.router[0]` (only the first endpoint is used), `routing.token`, `routing.peer_name`.

---

## 3. Routing connection (to router endpoint) **[confirmed]** (`mdg_mdg_routing_connection.lua`)

1. **Plain TCP** (no TLS yet) to `router[0].host : router[0].port`.
2. Send `[u16 len][0x00][mdg.routing.Handshake]`:
   `magic = 4036856981 (0xF09D8C95 → fixed32 bytes 95 8C 9D F0)`, `version_major = 1`, `version_minor = 0`,
   `token = routing.token` (bytes verbatim), `supported_protocols = ["tlsv1_2"]` (default; config never overrides).
3. Wait ≤10 s for a frame `[len][tag][pb]` with routing tags HANDSHAKE=0, PLEASEHOLD=1, GOAHEAD=2, ROUTINGERROR=3:
   * GOAHEAD `{selected_protocol}` → go to §4 (selected_protocol is ignored by the app; expect "tlsv1_2").
   * PLEASEHOLD (empty) → wait ≤10 s more for GOAHEAD or ROUTINGERROR.
   * ROUTINGERROR `{error}`: 0 NO_ERROR,1 SERVER_ERROR,2 SYNTAX_ERROR,3 INVALID_TOKEN,4 PEER_TIMEOUT,5 UNSUPPORTED_VERSION → fail.
   * anything else / close / timeout → fail (close socket).
4. Immediately after GOAHEAD the **same TCP socket** is upgraded to TLS (next bytes are the TLS ClientHello; the u16
   framing is applied to plaintext *inside* TLS from now on).

---

## 4. End-to-end peer TLS + cc_json

### 4.1 TLS **[confirmed]**
* Caller (us) = **TLS client** (`place_call` passes mode `"client"`); CC = **TLS server** (`incoming_call` → `"server"`).
* Same ssl_params as §1 plus `verifyext = "lsec_continue"`: a peer cert is required (`fail_if_no_peer_cert`) but chain
  errors are ignored (self-signed certs on both ends).
* Our client cert = the **app identity that the CC has paired**. The CC accepts us only if (`mdg_mdg_peer_comm.lua check_socket`):
  1. we present a cert;
  2. `subject_key_identifier(cert) == colonize(lowercase_hex(SHA1(subjectPublicKey BIT STRING)))`
     (`pubkey_digest` = `X509_pubkey_digest` + lowercase hex; disassembled @0xa5ee4) – i.e. SKI must be the RFC 5280 method-1 hash;
  3. the SKI is in the CC's pairing DB **and** the cert's public key (PEM) equals the stored one (`contains_public_key`).
  → Reuse `app_cert.pem`/`app_key.pem` from the paired phone (`/data/data/com.danfoss.linkapp/files/luadata/`), or
    pair a new identity (pairing protocol not covered here).
* The app does **not** verify the CC's cert against anything except the cipher/protocol checks; the CC's identity is
  implied by the MDG server routing on `peer_name`. Optional hardening: pin the CC cert stored in the phone's pairing file
  (`certificate` field) or check that its SKI == the peer id you called.

### 4.2 Framing inside TLS **[confirmed]**
Each message = `[u16 BE length][UTF-8 JSON]`, no protocol handshake, no tag byte. Max 65535 bytes per message
(`>I2` header; behaviour for larger CC responses unknown **[UNCERTAIN]**).

### 4.3 Request envelope (`jadx/S/i.java b()`) **[confirmed]**
```json
{"id": 7,
 "data": {"type": "JsonWebRequest",
          "data": {"method": "GET",
                   "uri": "/api/rooms",
                   "headers": ["Accept: application/json; charset=utf-8"],
                   "content": { ... }        // only if the request has a body (PUT/PATCH/POST)
}}}
```
* `id`: integer, app uses a counter starting at 1; echoed in the response.
* `uri`: path built with Android `Uri.Builder.appendPath` → leading "/" (e.g. `/api/rooms`, `/api/system/info`).
  The CC also accepts `":<port>/<path>"` to target a different local port (default 8000).
* CC side (`dec/mdg_cc_json.lua`, shipped in the same Lua bundle): converts to `http://127.0.0.1:8000<uri>`, forwards
  headers (`"Name: value"` strings, lower-cased), for POST/PUT/PATCH json-encodes `content` and sets
  `content-type: application/json; charset=utf-8` + `content-length`.

### 4.4 Response envelope (built in `mdg_cc_json.lua`, parsed in `jadx/S/g.java p()` / `S/n.java`) **[confirmed]**
```json
{"id":7,"cc_time":23,
 "data":{"type":"JsonWebResponse",
         "data":{"headers":[],"status":200,"statusDescription":"OK","content":<raw HTTP body>}}}
```
* `cc_time` = ms the CC spent on the local HTTP request (optional).
* `content` is the raw response body spliced in – if the body is empty the JSON is **invalid** (`"content":}}`);
  the app repairs it with `str.replace("\"content\":}}", "\"content\":{}}}")`. Do the same.
* Success iff `200 <= status < 300`; app treats `400` on GetWorld specially. CC returns
  `{"id":N,"data":{"type":"JsonWebResponse","data":{"status":503,"statusDescription":"Service Unavailable (from cc_json)"}}}`
  when its concurrency limit is hit.
* Responses may arrive out of order; match on `id`. Unknown/unsolicited messages: ignore.

---

## 5. Flow control & keepalive on the peer stream **[confirmed]**
* "cc_json-flow-control" is a **CC-internal semaphore** (capacity `cc_json_wip`, default **17** concurrent requests;
  waits 5 s then answers 503). No on-the-wire flow-control messages. Keep ≤ ~10 requests in flight.
* No keepalive messages exist. Idle limits:
  * app side closes the routed stream after **60 s** with no traffic either way (`routing_proc_loop`, `2*30 s`);
  * CC side: socket `recv_timeout = 150 s` and cc_json loop "too quiet" after **200 s** without a request → closes.
  → Either send a cheap request (e.g. `GET /api/system/status` or whatever the integration polls) every ≤50–60 s, or
    just reconnect (place_call again) when the stream is closed. Each place_call needs a fresh token/routing.
* CC accepts at most `max_routing_connections = 5` simultaneous routed calls; don't leak streams.

---

## 6. LAN alternative (direct, no MDG) **[confirmed in code; untested]**
The CC runs `enable_lan_comm=true`, `enable_mdns=true` (`dec/mdg_cc_main.lua`). The app uses it when it finds the CC on mDNS
(`place-call-local`, `jadx/S/n.java` → `U/*`):
* mDNS: service `_danfoss-mdg._tcp.local.`, instance name = CC peer id (colon form), host `<peer id>.local`.
* TCP connect to `<CC IP>:14641` (fixed port in `mdg_mdg_lan_comm.lua`), **TLS immediately** (same §1 profile,
  client mode, `lsec_continue`), then u16 framing:
  * client sends frame `E4 B7 A4 01 | u8 len | "cc_json"` (struct `"c4Bc0"`),
  * CC answers frame `E4 B7 A4 81` if it accepts (it closes the socket if it rejects / caller not paired),
  * then exactly the same cc_json JSON frames as §4.2–4.4.
* Same client-certificate/pairing checks as §4.1 apply. Same bp320r1/curve-29 TLS requirement.
This is likely the best primary transport for Home Assistant (no cloud dependency); use the MDG path as fallback.

---

## 7. Minimal Python client checklist
1. Load app identity: `app_key.pem` (EC brainpoolP320r1) + `app_cert.pem` (must have SKI = SHA1(pubkey)); CC peer id (lowercase, colon-separated).
2. Custom TLS 1.2 client supporting curve id 29 = brainpoolP320r1 (§1.1), client-cert auth, ECDHE-ECDSA-AES128-GCM-SHA256.
3. LAN: mDNS/known IP → §6. Otherwise:
4. Control: connect 77.66.11.90/92, 5.179.92.180/182 (:443) in parallel → TLS (verify chain against trusted-CAs.pem, no hostname, no SNI)
   → send handshake(1), set_peer_whitelist(20,[cc_id]) , ping(4); handle 5/11/21/3/2; ping every ≤108 s.
5. place_call(10, peer_name=cc_id, protocol="cc_json") → reply 11 OK → routing.
6. Router: plain TCP → routing Handshake(tag 0) → PleaseHold/GoAhead (10 s each) → TLS client on same socket.
7. Send/receive u16-framed JSON envelopes; fix empty `content`; correlate by `id`; keep traffic < 60 s idle or reconnect.

### Constants
| name | value |
|---|---|
| control magic | 4036857000 = 0xF09D8CA8 (fixed32, LE bytes `A8 8C 9D F0`), version 1.0 |
| routing magic | 4036856981 = 0xF09D8C95 (LE bytes `95 8C 9D F0`), version 1.0 |
| routing supported_protocols | `["tlsv1_2"]` |
| call protocol | `"cc_json"` |
| control ports | 443 |
| LAN port / handshake | 14641; client `E4B7A401`+len+proto, server `E4B7A481` |
| ping | every 108 s since last send; reply timeout 5 s; rx-silence limit 1000 s |
| place_call reply timeout | 9 s; routing GoAhead/PleaseHold timeouts 10 s + 10 s |
| peer idle | app 60 s, CC 150–200 s |
| CC concurrency | 17 in-flight (503 after 5 s wait); 5 routed connections |
| curve | brainpoolP320r1 = TLS named curve id **29 (0x001d)** (patched OpenSSL) |

## 8. Open questions / uncertainties
1. **MDG server TLS stack**: assumed it also uses curve id 29 / bp320r1 server cert (CA is bp320r1). Verify with a first ClientHello offering `[0x001d, 0x0017]` and inspect ServerKeyExchange. If the server picks 0x0017 (P-256) a stock TLS stack might work for the control leg (still needs a custom verify since the CA is bp320r1).
2. Whether the MDG server requires the caller to be on the CC's whitelist (CC sends its own set_peer_whitelist) → expect PEER_NOT_AVAILABLE if the identity is not paired.
3. Whether handshake properties are required/validated server-side (probably logging only).
4. `endpoint.host` encoding (text vs packed) – inferred text.
5. Behaviour for JSON messages > 65535 bytes (u16 header); not seen handled anywhere.
6. LAN path is implemented in the CC's config and app code but untested here; whether current CC firmware still listens on 14641.
7. The phone's `app_cert.pem` must be the one the CC paired; if lost, the remote/local pairing protocol (PACE, `mdg_pairing_procedure.lua`) must be reimplemented.
