"""
Core session lifecycle logic. Ties together db.py (persistence),
network_manager.py (iptables grant/revoke), and gpio_handler.py (coin
pulses) into the actual customer-facing behavior locked in STEP 1.

Covers:
- Device/session identity with MAC-randomization cookie fallback (rule #1)
- Additive rate stacking (rule #5)
- Real-time countdown + zero-balance cutoff, no grace period (rule #6)
- Power-loss/reboot recovery via wall-clock elapsed time (rule #7)
- Pause/Resume: unlimited pause duration, 30-day abandonment expiry that
  resets on each resume-then-pause cycle, MAC-device-bound, multiple
  cycles allowed, coin-while-paused adds to frozen balance without
  forcing a resume (item 15, locked Sessions 41/56/57/58)
"""

import logging
import threading
import time

import config
import db
import network_manager

logger = logging.getLogger("barathrum.session")


def _get_rate():
    """Pesos-per-pulse / minutes-per-pulse, overridable via the admin
    panel's config table -- placeholder defaults per rule #5 until PM
    locks a real production rate."""
    pesos = int(db.get_config(db.CFG_PESOS_PER_PULSE, config.DEFAULT_PESOS_PER_PULSE))
    minutes = int(db.get_config(db.CFG_MINUTES_PER_PULSE, config.DEFAULT_MINUTES_PER_PULSE))
    return pesos, minutes


# --- identity resolution (rule #1) --------------------------------------

def resolve_session(mac_address, cookie_token=None):
    """
    Returns the dict session row for this device, creating a fresh
    zero-balance one if neither the MAC nor the cookie match anything.

    Enforcement is MAC-based (iptables has to be); identity recovery has
    a cookie fallback for MAC-randomizing devices. If the MAC doesn't
    match anything but a valid, non-expired cookie does, the balance is
    transferred to the new MAC and the grant re-issued under it.
    """
    existing = db.get_session_by_mac(mac_address)
    if existing:
        return existing

    if cookie_token:
        by_cookie = db.get_session_by_token(cookie_token)
        if by_cookie and by_cookie["status"] != "expired":
            logger.info(
                "MAC-randomization fallback: reattaching session %s to new MAC %s",
                cookie_token, mac_address,
            )
            db.rebind_mac(cookie_token, mac_address)
            if by_cookie["status"] == "active" and by_cookie["remaining_seconds"] > 0:
                network_manager.grant_mac(mac_address)
            return db.get_session_by_token(cookie_token)

    # Brand new device, zero balance until they insert a coin.
    token = db.create_session(mac_address, remaining_seconds=0)
    return db.get_session_by_token(token)


# --- coin handling (rules #3, #4, #5) -----------------------------------

def handle_coin_pulse(session_token, pulse_count=1):
    """
    Called once per debounced coin pulse (or a batch, if the caller
    coalesces). Additive: adds straight to remaining_seconds whether the
    session is active, paused, or brand new -- resuming is always a
    separate, explicit customer action (item 15), never forced by a coin.
    """
    pesos_per_pulse, minutes_per_pulse = _get_rate()
    seconds_per_pulse = minutes_per_pulse * 60

    session = db.get_session_by_token(session_token)
    if session is None:
        raise ValueError(f"No session for token {session_token}")

    added_seconds = pulse_count * seconds_per_pulse
    db.add_remaining(session_token, added_seconds)

    db.record_transaction(
        mac_address=session["mac_address"],
        session_token=session_token,
        pulses=pulse_count,
        amount_pesos=pulse_count * pesos_per_pulse,
        minutes_granted=pulse_count * minutes_per_pulse,
    )

    session = db.get_session_by_token(session_token)  # re-fetch, updated balance

    # A brand-new or previously-zero session becomes active + granted the
    # moment it has a positive balance. A PAUSED session stays paused --
    # the coin just tops up the frozen balance (item 15's explicit rule).
    if session["status"] not in ("paused",) and session["remaining_seconds"] > 0:
        db.set_status(session_token, "active")
        network_manager.grant_mac(session["mac_address"])

    return session


# --- pause/resume (item 15) ---------------------------------------------

def pause_session(session_token):
    session = db.get_session_by_token(session_token)
    if session is None or session["status"] != "active":
        raise ValueError("Can only pause an active session.")
    db.set_status(session_token, "paused", paused_at=time.time())
    # Connection behavior during pause: device stays on the WiFi network,
    # only internet is cut -- so we revoke the FORWARD grant (no internet
    # passthrough) but do NOT touch hostapd/dnsmasq association at all,
    # the device remains associated to the AP throughout.
    network_manager.revoke_mac(session["mac_address"])
    logger.info("Session %s paused.", session_token)


def resume_session(session_token, requesting_mac_address):
    """
    Resume is tied to the same device that was used to pause (rule
    confirmed Session 58) -- the caller (portal_app.py) is responsible
    for only exposing the Resume button/action to the device whose MAC
    currently matches this session row; this function double-checks that
    invariant rather than trusting the caller blindly.
    """
    session = db.get_session_by_token(session_token)
    if session is None or session["status"] != "paused":
        raise ValueError("Can only resume a paused session.")
    if session["mac_address"] != requesting_mac_address:
        raise PermissionError(
            "Resume is bound to the device that paused this session; "
            "MAC does not match."
        )
    db.set_status(session_token, "active", paused_at=None)
    network_manager.grant_mac(session["mac_address"])
    logger.info("Session %s resumed.", session_token)


def check_pause_abandonment():
    """
    Run periodically (see main.py's background loop). Forfeits any
    paused session that has sat untouched for
    PAUSE_ABANDONMENT_EXPIRY_DAYS -- the 30-day clock is measured from
    `paused_at`, which is reset to a fresh timestamp on every
    resume-then-pause cycle (never accumulates across cycles), matching
    the "resets on each resume" decision locked Session 58.

    Forfeiture surfaces on the box's own portal (10.0.0.1), NOT the cloud
    dashboard -- the customer simply sees zero balance next time they
    open the portal to insert a coin. No separate notification mechanism
    exists for this in MVP.
    """
    threshold_seconds = config.PAUSE_ABANDONMENT_EXPIRY_DAYS * 86400
    now = time.time()
    for session in db.get_all_active_sessions():
        if session["status"] != "paused" or session["paused_at"] is None:
            continue
        if now - session["paused_at"] >= threshold_seconds:
            logger.info(
                "Session %s forfeited -- paused for >= %d days with no resume.",
                session["session_token"], config.PAUSE_ABANDONMENT_EXPIRY_DAYS,
            )
            db.update_remaining(session["session_token"], 0)
            db.set_status(session["session_token"], "expired")


# --- countdown + zero-balance cutoff (rule #6) --------------------------

def tick_active_sessions(elapsed_seconds=1):
    """
    Called once per second by main.py's countdown loop. Decrements every
    ACTIVE (not paused) session by elapsed_seconds; on hitting zero,
    revokes access immediately -- no grace period for MVP (rule #6).
    """
    for session in db.get_all_active_sessions():
        if session["status"] != "active":
            continue
        new_remaining = session["remaining_seconds"] - elapsed_seconds
        db.update_remaining(session["session_token"], new_remaining)
        if new_remaining <= 0:
            db.set_status(session["session_token"], "expired")
            network_manager.revoke_mac(session["mac_address"])
            logger.info(
                "Session %s hit zero -- access revoked immediately, no grace period.",
                session["session_token"],
            )


# --- power-loss / reboot recovery (rule #7) -----------------------------

def recover_sessions_on_boot():
    """
    Called once, early in main.py's startup, before the countdown loop
    or GPIO handling starts. Reloads every non-expired session and
    decrements it by REAL wall-clock time elapsed since last_updated_at
    (not a fresh countdown resume) -- so a reboot never grants free time,
    and never unfairly loses time beyond what genuinely elapsed.

    Paused sessions are NOT decremented here (pause explicitly freezes
    the countdown) -- only their abandonment-expiry check
    (check_pause_abandonment) cares about elapsed wall-clock time.
    """
    now = time.time()
    network_manager.apply_base_firewall_policy()

    for session in db.get_all_active_sessions():
        if session["status"] == "active":
            elapsed = max(0, now - session["last_updated_at"])
            new_remaining = session["remaining_seconds"] - elapsed
            db.update_remaining(session["session_token"], new_remaining)
            if new_remaining <= 0:
                db.set_status(session["session_token"], "expired")
                logger.info(
                    "Session %s expired while box was down (%.0fs elapsed).",
                    session["session_token"], elapsed,
                )
            else:
                network_manager.grant_mac(session["mac_address"])
                logger.info(
                    "Session %s recovered on boot -- %.0fs remaining "
                    "after %.0fs elapsed downtime.",
                    session["session_token"], new_remaining, elapsed,
                )
        elif session["status"] == "paused":
            # No grant re-issued -- paused sessions have no internet
            # access by design, this is unchanged by a reboot.
            logger.info(
                "Session %s remains paused across reboot (untouched).",
                session["session_token"],
            )


class BackgroundLoop:
    """Runs tick_active_sessions() and check_pause_abandonment() on
    fixed intervals in a daemon thread. Kept deliberately simple (a
    sleep loop, not a full scheduler) since this only ever needs to run
    inside a single long-lived agent process."""

    def __init__(self, tick_interval_seconds=1, abandonment_check_interval_seconds=3600):
        self.tick_interval = tick_interval_seconds
        self.abandonment_check_interval = abandonment_check_interval_seconds
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        last_abandonment_check = 0.0
        while not self._stop.is_set():
            tick_active_sessions(self.tick_interval)
            now = time.time()
            if now - last_abandonment_check >= self.abandonment_check_interval:
                check_pause_abandonment()
                last_abandonment_check = now
            time.sleep(self.tick_interval)
