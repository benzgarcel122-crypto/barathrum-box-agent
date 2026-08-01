"""
Central configuration for the Barathrum box agent.

Values here are DEFAULTS ONLY. Anything an operator can change from the
on-box admin panel (rate, GPIO pins, arm/disarm timing, SSID/password,
admin credential hash) lives in the `config` SQLite table at runtime
(see db.py) and overrides these defaults on load. This file exists so
there's always a safe fallback even on a completely fresh SD card before
the config table has been populated.

MPD cross-references are noted inline so a future session can trace every
number back to the locked design decision that produced it.
"""

import os

# --- Paths -------------------------------------------------------------

DATA_DIR = os.environ.get("BARATHRUM_DATA_DIR", "/var/lib/barathrum")
DB_PATH = os.path.join(DATA_DIR, "barathrum.sqlite3")

# --- Network (STEP 1 rule #2) -------------------------------------------

# Ethernet = WAN (to the ISP modem). USB WiFi adapter = LAN/AP (customer-
# facing hostapd interface) -- the Orange Pi One has NO onboard WiFi
# (confirmed Session 59), so LAN_IFACE below assumes a USB adapter is
# physically present. Adjust to match `ip link` output on the real unit;
# common USB WiFi adapters enumerate as wlan0, wlan1, etc.
WAN_IFACE = os.environ.get("BARATHRUM_WAN_IFACE", "eth0")
LAN_IFACE = os.environ.get("BARATHRUM_LAN_IFACE", "wlan0")

# "onboard_hostapd" (default): box runs its own hostapd directly on a USB WiFi
# dongle plugged into LAN_IFACE. "external_ap": a dedicated AP device (fed by a
# wired connection on LAN_IFACE, e.g. via a USB-to-LAN adapter) does the WiFi
# broadcasting itself -- this box does not run hostapd at all in that mode.
WIFI_MODE = os.environ.get("BARATHRUM_WIFI_MODE", "onboard_hostapd")

GATEWAY_IP = "10.0.0.1"          # locked Session 28, MPD line ~2154
DHCP_RANGE_START = "10.0.0.4"    # .1 = gateway, .2/.3 reserved
DHCP_RANGE_END = "10.0.0.254"
DHCP_LEASE_TIME = "12h"

# --- Coin acceptor / GPIO (STEP 1 rules #3, #4) -------------------------

DEFAULT_COIN_PIN = 7  # BOARD numbering; MUST be re-verified against the
                       # Orange Pi One's own pinout, NOT a Raspberry Pi or
                       # other Orange Pi model's pinout (MPD flags the pin
                       # layout as reportedly mirrored/flipped 180 degrees
                       # relative to Orange Pi PC -- confirm on real board
                       # before wiring anything).
DEFAULT_RELAY_PIN = 11

# Pulse polarity: active-low with internal pull-up is the default
# convention for optocoupler/relay-based coin acceptor outputs, but this
# is explicitly configurable since some hardware runs active-high.
DEFAULT_PULSE_ACTIVE_LOW = True

# Debounce: minimum pulse width (seconds) to count as a real coin pulse
# rather than electrical noise. Tune against the real acceptor's datasheet
# or by observation -- this default is a conservative starting point, not
# a measured value.
DEBOUNCE_SECONDS = 0.05

# Arm/disarm timing (STEP 1 rule #4):
ARM_IGNORE_WINDOW_SECONDS = 1.5   # suppress power-up phantom-pulse issue
ARM_ACCEPT_WINDOW_SECONDS = 60    # resets on each valid coin

# --- Rate model (STEP 1 rule #5) ----------------------------------------

# PLACEHOLDER default for build/testing only -- actual production
# peso-to-minutes rate is an explicit open PM/business decision, not yet
# locked. Do not treat this number as final; it is overridden by the
# `config` table's own rate entry, which the admin panel writes to.
DEFAULT_PESOS_PER_PULSE = 1
DEFAULT_MINUTES_PER_PULSE = 5

# --- Session lifecycle (STEP 1 rule #6) ---------------------------------

SESSION_ENDING_SOON_THRESHOLD_SECONDS = 5 * 60  # portal UI amber/red warning

# --- Pause/Resume (item 15, locked Sessions 56-58) ----------------------

# Deliberately NOT a fixed pause-duration cap (unlike common competitor
# 30-60 min pattern) -- a paused session can sit for any length of time
# and still resume with exact remaining balance intact. The only limit is
# the abandonment expiry below.
PAUSE_ABANDONMENT_EXPIRY_DAYS = 30  # resets fresh on each resume-then-pause

# --- Setup Wizard / pairing (STEP 1 rule #11) ---------------------------

SETUP_SSID_PREFIX = "Barathrum-Setup-"  # + last 4 chars of box MAC address

# Cloud dashboard API base URL, used only during Setup Wizard license
# validation. NOT YET BUILT on the backend side as of this writing --
# machines/ has no urls.py exposing a box-pairing endpoint yet (confirmed
# by direct repo inspection, Session 60). This is a real, separate open
# item -- see README.md's "Known gaps" section.
DASHBOARD_API_BASE_URL = os.environ.get(
    "BARATHRUM_DASHBOARD_API_BASE_URL", "https://barathrum-backend-production.up.railway.app"
)

# --- Admin panel (STEP 1 rule #9) ---------------------------------------

ADMIN_SESSION_COOKIE_NAME = "barathrum_admin_session"
CUSTOMER_SESSION_COOKIE_NAME = "barathrum_session_token"
CUSTOMER_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 90  # 90 days; long-lived
                                                       # by design, per rule #1

# Admin login brute-force lockout (STEP 1 tracker row 25). Single global admin credential per
# box -- unlike OTPCode centrally (many codes/phone numbers at once), there is exactly one admin
# password per box, so a single persistent counter is the correct match here, not anything
# per-IP or per-session.
ADMIN_LOGIN_MAX_FAILED_ATTEMPTS = 5   # same number OTPCode already uses centrally, for consistency
ADMIN_LOGIN_LOCKOUT_SECONDS = 300     # 5 minutes; PM can tune this, not fixed in stone
