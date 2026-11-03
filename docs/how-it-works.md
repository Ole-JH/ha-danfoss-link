# How it works

```
Home Assistant ──mTLS──► Danfoss MDG relay (77.66.11.x / 5.179.92.x : 443)
               ══ end-to-end TLS tunnel ══► Link CC ──► http://127.0.0.1:8000/api/...
```

The CC has no local API: it closes all LAN ports and only talks to the relay. To get
through, Home Assistant needs an identity (a client certificate and key) that your CC
has paired with. There are two ways to get one:

- **Pair with the code from the CC (default).** Home
  Assistant creates its own identity and pairs it with the CC using the code the CC
  shows, the way the app pairs a new phone. No Android device needed, and your phone
  app keeps its own pairing.
- **[Use exported app credentials](exported-credentials.md).** Borrow the identity of an
  already paired Android app (needs root or an emulator). Use this if pairing does not
  work for you.

Technical details are in [NOTICE.md](../NOTICE.md), [protocol.md](protocol.md),
[connection-spec.md](connection-spec.md) and the docstrings in `custom_components/danfoss_link/api/`.

## Pairing

Home Assistant is paired as its own app (it introduces itself to the CC as "Home
Assistant"), separate from your phone's pairing, so the phone app keeps working
alongside it.

- The CC must be connected to the internet: pairing goes through the Danfoss cloud.
- Codes starting with `0000` are for pairing on the local network, which is not
  supported. Use a code for pairing through the cloud.
- If pairing worked but the first connection fails, the form lets you retry without a
  new code.
- If pairing doesn't work for you at all, please open an issue with debug logs and use
  the [exported app credentials](exported-credentials.md) in the
  meantime.

## Known limitations

- **Cloud dependent.** Danfoss still ships CC firmware (4.2.1797, Feb 2025) and the
  relay works today, but there is no promise it stays up.
- **Pairing uses the cloud relay only.** Remote pairing (PACE on brainpoolP320r1)
  works against a real CC; LAN pairing codes (`0000...`) are not supported.
- **Peer certificate not validated.** Like the app, the integration uses a custom TLS
  stack (the CC's brainpoolP320r1 certificates are unsupported by OpenSSL). It checks the
  handshake signatures but does not verify the relay's or the CC's certificate chain,
  so a man-in-the-middle on the network path is not ruled out.
- **Tested on one setup so far.** It is confirmed on a real Link CC, but the message
  formats come from reverse engineering and other device mixes may behave differently.
  Please open an issue with debug logs if anything doesn't work with your CC.

## Alternatives

If you would rather leave the cloud entirely: plain Z-Wave "Living Connect" thermostats
(014G0013 / LC-13) can be factory reset and included directly in Z-Wave JS. Link-branded
devices (014G0001/0002 thermostats, Link RS/FT/HC) use a proprietary protocol and only
work with the CC.
