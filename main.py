"""
Entrypoint for the Barathrum box agent. Run as root via the systemd unit
(see systemd/barathrum-agent.service) on the real box, through gunicorn
with --preload so this module's bootstrap() runs exactly once before the
WSGI app starts serving -- NOT inside `if __name__ == "__main__"`, which
would only fire for direct `python main.py` execution and be silently
skipped when gunicorn imports this module instead. This was a real bug
caught and fixed during the initial build (flagged here rather than
silently corrected, since it's exactly the kind of mistake worth a
future session knowing was made and why).

Startup order matters here:
1. DB init (idempotent, safe on every boot)
2. Session recovery from real elapsed wall-clock time (rule #7) --
   MUST happen before the countdown loop starts, and before GPIO/coin
   handling comes online, so nothing double-counts or double-grants.
3. Base firewall policy (fail-closed default DROP) -- also happens
   inside recover_sessions_on_boot(), kept here as an explicit ordering
   note rather than a hidden side effect.
4. GPIO coin acceptor wiring.
5. Background tick/abandonment-check loop.
6. Flask app object exposed at module level as `app` -- gunicorn's
   WSGI target (see the systemd unit's `main:app`).
"""

import logging
import os
import secrets

import config
import db
import portal_app
import session_manager
from gpio_handler import CoinAcceptor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("barathrum.main")

app = portal_app.app  # gunicorn's WSGI target: `main:app`

_bootstrapped = False


def _load_or_create_flask_secret():
    """Persisted across restarts so admin login sessions survive an
    agent restart (not a reboot -- reboot triggers full session recovery
    anyway, a Flask-secret change only matters for the admin cookie)."""
    existing = db.get_config("flask_secret_key")
    if existing:
        return existing
    fresh = secrets.token_hex(32)
    db.set_config("flask_secret_key", fresh)
    return fresh


def bootstrap():
    """Idempotent -- guarded so it only ever runs once even if this
    module gets imported more than once in the same process (gunicorn
    with --preload imports it once in the master before forking, which
    is exactly the desired behavior with --workers 1)."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True

    os.makedirs(config.DATA_DIR, exist_ok=True)

    logger.info("Barathrum box agent starting.")
    db.init_db()

    logger.info("Recovering sessions from real elapsed downtime...")
    session_manager.recover_sessions_on_boot()

    app.secret_key = _load_or_create_flask_secret()

    coin_pin = int(db.get_config(db.CFG_COIN_PIN, config.DEFAULT_COIN_PIN))
    relay_pin = int(db.get_config(db.CFG_RELAY_PIN, config.DEFAULT_RELAY_PIN))
    active_low_raw = db.get_config(db.CFG_PULSE_ACTIVE_LOW)
    active_low = (
        config.DEFAULT_PULSE_ACTIVE_LOW if active_low_raw is None else active_low_raw == "1"
    )

    def _on_coin():
        # This callback runs on the GPIO watchdog thread -- the session
        # this pulse belongs to is whichever session is CURRENTLY armed
        # (see gpio_handler.CoinAcceptor.arm()'s docstring for the
        # documented, accepted race behavior when two devices both tap
        # "Insert Coin" within the same window: most-recent arm() wins).
        # Read fresh here rather than captured at callback-registration
        # time, since `acceptor` itself tracks the live value.
        armed_token = acceptor.get_armed_session_token()
        if armed_token is None:
            logger.warning(
                "Coin pulse received while no session_token was armed -- "
                "coin ignored, no balance updated. Should not happen in "
                "normal operation (arm() is always called with a token "
                "from api_arm_coin_acceptor before pulses can occur)."
            )
            return
        logger.info("Coin pulse received for session_token=%s.", armed_token)
        try:
            session_manager.handle_coin_pulse(armed_token)
        except ValueError:
            logger.exception(
                "handle_coin_pulse failed for session_token=%s -- session "
                "may have been deleted/expired mid-arm.", armed_token,
            )

    acceptor = CoinAcceptor(
        coin_pin=coin_pin,
        relay_pin=relay_pin,
        active_low=active_low,
        on_coin=_on_coin,
    )
    portal_app.attach_coin_acceptor(acceptor)

    loop = session_manager.BackgroundLoop()
    loop.start()

    logger.info("Bootstrap complete.")


bootstrap()  # runs at import time -- correct under both gunicorn
             # --preload and direct `python main.py` execution


if __name__ == "__main__":
    # Local/dev convenience only -- production uses gunicorn via the
    # systemd unit, not Flask's dev server.
    logger.info("Starting Flask DEV server on %s:80 (local/dev only).", config.GATEWAY_IP)
    app.run(host="0.0.0.0", port=80, debug=False)
