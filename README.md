# Barathrum Box Agent (STEP 1)

Offline-capable coin-op WiFi box agent for the Orange Pi One. See `MPD_barathrum.md`'s STEP 1
section for the full locked design; this file covers what's actually in this repo, its current
state, and what's still genuinely open.

**Verification status: logic-tested in dry-run/simulated mode only, never on real hardware.**
Nothing here is "confirmed" in this project's own sense until it's flashed onto a real Orange Pi
One and booted with a real coin acceptor and USB WiFi adapter wired up.

## Quick start (dev machine, no hardware)

```
pip install Flask==3.0.3 Werkzeug==3.0.3
BARATHRUM_DRY_RUN=1 BARATHRUM_DATA_DIR=/tmp/barathrum-dev python3 -c "import main"
```

Run the automated test suite:

```
BARATHRUM_DRY_RUN=1 python3 -m unittest tests -v
```

## What's built

- `config.py` — every tunable value, commented with the MPD rule/session that locked it.
- `db.py` — SQLite schema + CRUD (sessions, transactions, config).
- `gpio_handler.py` — coin pulse debounce + arm/disarm window, `OPi.GPIO`-based with a simulated
  fallback off real hardware.
- `network_manager.py` — iptables MAC grant/revoke (fail-closed default-DROP), hostapd/dnsmasq
  config rendering, `BARATHRUM_DRY_RUN=1` mode for testing without root/real network tools.
- `session_manager.py` — identity resolution, additive rate stacking, Pause/Resume, zero-balance
  cutoff, power-loss recovery via wall-clock elapsed time.
- `portal_app.py` — Flask app: customer captive portal (7 UI states), on-box admin panel, 4-screen
  Setup Wizard.
- `main.py` — entrypoint; bootstrap runs at import time so it fires correctly under both
  `gunicorn --preload` (production) and direct `python main.py` (dev).
- `tests.py` — 25 dry-run tests covering coin insertion, additive stacking, the arm-race edge
  case, Pause/Resume (all mechanics), zero-balance cutoff, power-loss recovery math, Setup Wizard,
  hostapd open-vs-secured rendering, and admin panel auth.

## Bugs found and fixed this session (not present in the original handoff)

1. **The live coin-pulse callback never actually updated any session.** `main.py`'s `_on_coin()`
   only logged a line — `session_manager.handle_coin_pulse()` was fully built and tested in
   isolation but was never wired to the real GPIO pulse path. A real coin would have done nothing.
   Fixed: `gpio_handler.CoinAcceptor.arm()` now takes the requesting session's token and tracks it
   as `_armed_session_token`; `_on_coin()` reads it via `get_armed_session_token()` and calls
   `handle_coin_pulse()` for real. See `gpio_handler.py`'s `arm()` docstring for the documented,
   accepted behavior when two devices both arm within the same window (most-recent arm wins —
   never double-grants, never silently drops a pulse).
2. **`hostapd.conf.template` always rendered the WPA block, even for the open Setup Wizard
   network.** hostapd rejects `wpa=2` with an empty `wpa_passphrase` outright at startup — the
   temporary setup network would never have come up. Fixed: the WPA lines are now built
   conditionally in `network_manager.render_hostapd_config()` and only substituted in when a real
   passphrase is passed.

## Known gaps — genuinely still open

### Requires real hardware (cannot be done from a dev machine)
- Confirm `config.DEFAULT_COIN_PIN` / `DEFAULT_RELAY_PIN` against the **real Orange Pi One's own
  pinout** — community sources (see `HARDWARE_RESEARCH_NOTES.md`) independently confirm the header
  is reported 180°-flipped relative to Orange Pi PC/Raspberry Pi. Do not trust the placeholder pin
  numbers in `config.py`.
- Confirm `OPi.GPIO` actually behaves as expected on the real board — only ever run in simulated
  mode so far.
- Order, wire, and confirm the real interface name (`ip link`) of a USB WiFi adapter — Orange Pi
  One has no onboard WiFi at all. `config.LAN_IFACE` defaults to `wlan0`, a guess.
- `config.DEBOUNCE_SECONDS` (0.05s) and `config.DEFAULT_PULSE_ACTIVE_LOW` are placeholders, not
  measured against a real coin acceptor.
- Build and flash an actual `.img`/`.img.xz` via real Balena Etcher onto a real SD card, boot it —
  see `HARDWARE_RESEARCH_NOTES.md` for the concrete build-tooling answer (Armbian's own build
  framework officially supports this exact board).

### Code-level, low priority, deliberately left as-is
- `main.py`'s `_on_coin()` race handling: documented "most recent arm() wins" behavior (see above)
  — a real design decision, not an oversight, but worth a PM confirmation if it ever becomes a
  real complaint pattern.
- `dnsmasq.conf.template` blanket-redirects all DNS rather than scoping to known OS
  connectivity-check domains. Fine since the portal is fully self-hosted with no external assets;
  worth tightening only if that changes.

### Separate task, not this one
- Setup Wizard Screen 1 (license key validation) is stubbed — accepts any non-empty key. The
  Django cloud backend has no exposed box-pairing/license-validation endpoint yet (confirmed by
  direct repo inspection — no `machines/urls.py`). Needs its own scoped cloud-side task.

## Deployment

Real deploy target is a single flashable Armbian image (see `HARDWARE_RESEARCH_NOTES.md`), not a
script copied onto an existing OS install. `systemd/barathrum-agent.service` is the systemd unit
that runs this via gunicorn with `--preload` in that image.
