# Development

## Dev container

The repository has a [dev container](https://containers.dev) (`.devcontainer/`), the
usual setup for Home Assistant custom integrations. Open the folder in VS Code and choose
**Reopen in Container** (needs Docker). On first start it runs `scripts/setup`, which
installs Home Assistant, its test package and the linters into `.venv`.

- `scripts/test` runs the linters and the test suite, like CI does.
- `scripts/develop` starts Home Assistant on <http://localhost:8123> with this
  integration loaded straight from the checkout. Its config lives in `config/`
  (only `config/configuration.yaml` is committed).

To try an upcoming Home Assistant release, install its beta and run the tests again:

```bash
HA_VERSION=2026.12.0b0 scripts/setup
scripts/test
```

## Without the dev container

```bash
HA_VERSION=2026.11.0 scripts/setup   # makes .venv; needs Python 3.14.2+ for HA 2026.11
source .venv/bin/activate
scripts/test
```

Home Assistant 2026.11 and newer publish their own test package, `pytest-homeassistant`.
Older releases are tested with `pytest-homeassistant-custom-component` instead
(`pip install -r requirements_test.txt`). The tests work with both: helpers are
imported through `tests/common.py`. CI runs the suite on the oldest release `hacs.json`
allows (2025.10.0), on the newest one `pytest-homeassistant-custom-component` supports,
and on 2026.11.0.

The tests run the real client against `tests/fake_mdg.py`, a local fake of the relay
and CC (including the CC's side of pairing) that uses OpenSSL on the server side.

The icon is original artwork, not the Danfoss logo. `scripts/generate_icons.py`
renders it into `custom_components/danfoss_link/brand/` (shown by Home Assistant 2026.3
and newer) and `docs/images/` (README header and GitHub social preview).
