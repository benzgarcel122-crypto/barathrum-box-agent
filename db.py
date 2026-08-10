"""
SQLite persistence layer (STEP 1 rule #10 schema, rule #7 power-loss
behavior).

Design notes:
- Remaining balance lives here, not just in memory, specifically so a
  power loss / reboot doesn't grant free time or unfairly lose time
  (rule #7): on boot, main.py reloads every session with
  remaining_seconds > 0 and re-applies iptables grants, decrementing
  based on real wall-clock elapsed time while the box was down.
- `status` on a session is one of: 'active', 'paused', 'expired'.
  'expired' covers both the normal zero-balance end AND the 30-day
  pause-abandonment forfeiture (item 15) -- both are terminal, the
  session is just kept as a row for the transaction/audit history
  rather than deleted, mirroring the cloud dashboard's own preference
  for an auditable trail over silent deletion where practical.
"""

import sqlite3
import time
import uuid
from contextlib import contextmanager

import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac_address TEXT NOT NULL,
    session_token TEXT NOT NULL UNIQUE,
    remaining_seconds INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active | paused | expired
    paused_at REAL,                          -- unix timestamp, NULL if not paused
    last_updated_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_mac ON sessions(mac_address);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(session_token);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac_address TEXT NOT NULL,
    session_token TEXT NOT NULL,
    pulses_counted INTEGER NOT NULL,
    amount_pesos INTEGER NOT NULL,
    minutes_granted INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    minutes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    redeemed_at REAL,                -- NULL until redeemed
    redeemed_by_mac TEXT,
    redeemed_session_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_vouchers_code ON vouchers(code);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Config keys stored in the `config` table (overrides config.py defaults)
CFG_PESOS_PER_PULSE = "pesos_per_pulse"
CFG_MINUTES_PER_PULSE = "minutes_per_pulse"
CFG_COIN_PIN = "coin_pin"
CFG_RELAY_PIN = "relay_pin"
CFG_PULSE_ACTIVE_LOW = "pulse_active_low"
CFG_CUSTOMER_SSID = "customer_ssid"
CFG_CUSTOMER_WIFI_PASSWORD = "customer_wifi_password"
CFG_ADMIN_PASSWORD_HASH = "admin_password_hash"
CFG_LICENSE_KEY = "license_key"
CFG_SETUP_COMPLETE = "setup_complete"  # "1" once Setup Wizard has run
# Admin login brute-force lockout (STEP 1 tracker row 25). Persisted here rather than held in
# memory, same reasoning as everything else in this table -- an in-memory-only counter would
# reset on every box reboot, defeating the point (an attacker could just force/wait for a reboot
# to reset their attempt budget).
CFG_ADMIN_LOGIN_FAILED_ATTEMPTS = "admin_login_failed_attempts"
CFG_ADMIN_LOGIN_LOCKED_UNTIL = "admin_login_locked_until"  # unix timestamp, empty/absent = not locked
CFG_LICENSE_POINTS = "license_points"  # cached locally; "0" until Setup Wizard bind or first sync


def get_connection():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Idempotent -- safe to call on every boot before anything else runs."""
    with cursor() as cur:
        cur.executescript(SCHEMA)


# --- config table helpers ------------------------------------------------

def get_config(key, default=None):
    with cursor() as cur:
        row = cur.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key, value):
    with cursor() as cur:
        cur.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def is_setup_complete():
    return get_config(CFG_SETUP_COMPLETE) == "1"


# --- sessions --------------------------------------------------------------

def new_session_token():
    return uuid.uuid4().hex


def create_session(mac_address, remaining_seconds):
    """
    Always created as 'active' regardless of remaining_seconds -- this is
    deliberate, not an oversight (an earlier version of this function set
    status='expired' for zero-balance new sessions, which broke
    get_session_by_mac()'s `status != 'expired'` filter and caused a new
    duplicate row to be created on every single request instead of the
    same fresh session being found and reused -- caught during
    smoke-testing, reverted). 'active' here just means "the current,
    reusable session row for this device," not "currently granted
    internet access" -- the actual grant is driven by remaining_seconds
    and the iptables rule, not this status column alone. See
    portal_app.api_session_status()'s ending_soon calculation for the
    corresponding fix that was needed on the read side instead.
    """
    now = time.time()
    token = new_session_token()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO sessions "
            "(mac_address, session_token, remaining_seconds, status, "
            " paused_at, last_updated_at, created_at) "
            "VALUES (?, ?, ?, 'active', NULL, ?, ?)",
            (mac_address, token, remaining_seconds, now, now),
        )
    return token


def get_session_by_mac(mac_address):
    """Most recent non-expired session for this MAC, if any."""
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM sessions WHERE mac_address = ? AND status != 'expired' "
            "ORDER BY created_at DESC LIMIT 1",
            (mac_address,),
        ).fetchone()
        return dict(row) if row else None


def get_session_by_token(token):
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM sessions WHERE session_token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def get_all_active_sessions():
    """Used on boot to reload + re-grant, and by the admin Sessions tab."""
    with cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM sessions WHERE status IN ('active', 'paused')"
        ).fetchall()
        return [dict(r) for r in rows]


def update_remaining(token, remaining_seconds):
    with cursor() as cur:
        cur.execute(
            "UPDATE sessions SET remaining_seconds = ?, last_updated_at = ? "
            "WHERE session_token = ?",
            (max(0, remaining_seconds), time.time(), token),
        )


def add_remaining(token, extra_seconds):
    """Additive stacking (rule #5) -- also used for 'insert coin while
    paused adds to frozen balance' (item 15)."""
    with cursor() as cur:
        cur.execute(
            "UPDATE sessions SET remaining_seconds = remaining_seconds + ?, "
            "last_updated_at = ? WHERE session_token = ?",
            (extra_seconds, time.time(), token),
        )


def set_status(token, status, paused_at=None):
    with cursor() as cur:
        cur.execute(
            "UPDATE sessions SET status = ?, paused_at = ?, last_updated_at = ? "
            "WHERE session_token = ?",
            (status, paused_at, time.time(), token),
        )


def rebind_mac(token, new_mac_address):
    """Session-token cookie fallback (rule #1) -- device reconnected with a
    different (rotated) MAC but presented a valid cookie. Transfer the
    session to the new MAC rather than creating a duplicate."""
    with cursor() as cur:
        cur.execute(
            "UPDATE sessions SET mac_address = ?, last_updated_at = ? "
            "WHERE session_token = ?",
            (new_mac_address, time.time(), token),
        )


def record_transaction(mac_address, session_token, pulses, amount_pesos, minutes_granted):
    with cursor() as cur:
        cur.execute(
            "INSERT INTO transactions "
            "(mac_address, session_token, pulses_counted, amount_pesos, "
            " minutes_granted, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (mac_address, session_token, pulses, amount_pesos, minutes_granted, time.time()),
        )


def get_todays_earnings_pesos():
    midnight = time.time() - (time.time() % 86400)
    with cursor() as cur:
        row = cur.execute(
            "SELECT COALESCE(SUM(amount_pesos), 0) AS total FROM transactions "
            "WHERE timestamp >= ?",
            (midnight,),
        ).fetchone()
        return row["total"]


# --- vouchers (item 10) -----------------------------------------------------

def create_voucher(code, minutes):
    """Raises sqlite3.IntegrityError if code already exists -- caller
    (portal_app.py's admin route) is responsible for retrying with a
    fresh code on collision."""
    with cursor() as cur:
        cur.execute(
            "INSERT INTO vouchers (code, minutes, created_at) VALUES (?, ?, ?)",
            (code, minutes, time.time()),
        )


def get_voucher_by_code(code):
    with cursor() as cur:
        row = cur.execute(
            "SELECT * FROM vouchers WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def redeem_voucher(code, mac_address, session_token):
    """
    Atomic redeem: the WHERE redeemed_at IS NULL clause means this can
    only ever succeed once for a given code, even under a race between
    two near-simultaneous redemption attempts -- the second call's
    UPDATE simply matches zero rows. Returns True if THIS call was the
    one that redeemed it, False if it was already redeemed (by a prior
    call or this same race).
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE vouchers SET redeemed_at = ?, redeemed_by_mac = ?, "
            "redeemed_session_token = ? WHERE code = ? AND redeemed_at IS NULL",
            (time.time(), mac_address, session_token, code),
        )
        return cur.rowcount == 1


def get_all_vouchers():
    """Newest first -- used by the admin Vouchers page."""
    with cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM vouchers ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
