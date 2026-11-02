# Changelog

Each release's GitHub release notes are taken from its section below. Add a
`## <version>` section matching the version in `manifest.json` before the
release PR to `main` is merged.

## 0.3.2

### Fixes
- A room you switch to Manual and then back to Schedule while the house is *At home* now gets its at-home temperature again. Before, the Danfoss controller quietly left that room out of *At home*, so it heated to its away temperature even though the house said At home.
- The House state sensor shows **Manual** instead of *unknown* when the CC reports a manual house state.
- On/off devices (e.g. floor-heating relays) show as unavailable while the CC reports "no response from unit" for them, instead of looking like they switched when nothing happened.

### Improvements
- Each thermostat shows its weekly schedule: the `schedule_today` attribute lists today's *at home* periods (e.g. `06:00-08:00`, `16:00-22:30`) and `schedule` lists them for every weekday. The rest of the day is the away (setback) temperature.
- The House mode select has a new `at_home_rooms` attribute listing the rooms the current *At home* period covers.
- Danish names for the House state values.

No entity IDs changed and no reconfiguration is needed.

## 0.3.1

### Fixes
- Short Danfoss relay drops no longer make entities unavailable. When the CC is briefly unreachable (`PEER_NOT_AVAILABLE`), the integration keeps the last known state for up to 5 minutes and retries every 15 seconds before marking entities unavailable. (#5)
- No more "deprecated `via_device` parameter" warnings on Home Assistant 2026.10 and later; rooms now link to the CC with `via_device_id`. (#3)
- README: the CC Wi-Fi signal sensor only appears on CCs that report it; firmware 4.2.1797 does not. (#4)

No entity IDs changed and no reconfiguration is needed.
