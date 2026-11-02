# Changelog

Each release's GitHub release notes are taken from its section below. Add a
`## <version>` section matching the version in `manifest.json` before the
release PR to `main` is merged.

## 0.3.2

### Fixes
- Rooms switched back from Manual to Schedule now rejoin an active *At home* period. Before, the CC dropped a room from *At home* when it went to Manual and never added it back, so it ran its away setpoint while the house said At home. Selecting *At home* again also re-covers every room, and *At home* is now always sent with every room so it stays open-ended instead of ending at the next schedule period.
- The House state sensor shows **Manual** instead of *unknown* when the CC reports a manual house state.
- On/off devices (e.g. floor-heating relays) show as unavailable while the CC reports "no response from unit" for them, instead of looking like they switched when nothing happened.

### Improvements
- The House mode select has a new `at_home_rooms` attribute listing the rooms the current *At home* period covers.
- Danish names for the House state values.

No entity IDs changed and no reconfiguration is needed.

## 0.3.1

### Fixes
- Short Danfoss relay drops no longer make entities unavailable. When the CC is briefly unreachable (`PEER_NOT_AVAILABLE`), the integration keeps the last known state for up to 5 minutes and retries every 15 seconds before marking entities unavailable. (#5)
- No more "deprecated `via_device` parameter" warnings on Home Assistant 2026.10 and later; rooms now link to the CC with `via_device_id`. (#3)
- README: the CC Wi-Fi signal sensor only appears on CCs that report it; firmware 4.2.1797 does not. (#4)

No entity IDs changed and no reconfiguration is needed.
