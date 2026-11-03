# Development

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The tests run the real client against `tests/fake_mdg.py`, a local fake of the relay
and CC (including the CC's side of pairing) that uses OpenSSL on the server side.

The icon is original artwork, not the Danfoss logo. `scripts/generate_icons.py`
renders it into `custom_components/danfoss_link/brand/` (shown by Home Assistant 2026.3
and newer) and `docs/images/` (README header and GitHub social preview).
