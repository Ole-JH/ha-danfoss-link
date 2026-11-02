# Troubleshooting

| Error | Meaning |
|---|---|
| *No CC is waiting for this code* | The code expired or was mistyped (everything but the last five characters identifies the CC). Start pairing on the CC again. |
| *The CC rejected the code* | The last five characters of the code didn't match. Check the code and retry, or get a new one. |
| *Codes starting with 0000...* | That is a local-network pairing code, which is not supported. |
| *Invalid credentials* | Exported credentials only: a PEM is incomplete, the key doesn't belong to the certificate, or the peer ID isn't 20 hex bytes. |
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
