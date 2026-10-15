# Credits

The protocol knowledge in this integration comes from community reverse engineering:

- **[dfhdsrfjgdf/ha-danfoss-link](https://github.com/dfhdsrfjgdf/ha-danfoss-link)** (MIT):
  the MDG control/routing message layout, the cc_json framing, the CC REST API and
  the TLS quirks (brainpoolP320r1 certificates, the app's ClientHello shape). The
  TLS handshake in `custom_components/danfoss_link/api/tls.py` is derived from its
  `tlslib.py`; the rest of this integration is a rewrite.
- **[Sonic-Amiga/opensdg](https://github.com/Sonic-Amiga/opensdg)** and the openHAB
  community thread on Danfoss Link, for the MDG host list and the early protocol notes.

Danfoss and Danfoss Link are trademarks of Danfoss A/S. This project is not
affiliated with Danfoss or Trifork.
