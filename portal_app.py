"""
Flask app serving three distinct surfaces on the box, all on the same
gateway IP (10.0.0.1):

1. Customer-facing captive portal -- STEP 1 rule #13 (7 states, added
   Pause/Resume states Session 58).
2. On-box local admin panel -- rule #9 / #12.
3. First-boot Setup Wizard -- rule #11.

Identity: the customer's MAC address is read from the connection itself
(see get_client_mac() -- on Linux this means reading the ARP/neighbor
table for the request's source IP, since Flask/WSGI has no direct MAC
visibility; this requires the app to run with sufficient privilege to
read /proc/net/arp or use `ip neigh`). The long-lived session-token
cookie (rule #1's fallback) is set on every response so a MAC-rotating
device can still be recognized on its next connection.
"""

import logging
import re
import secrets
import sqlite3
import subprocess
import time
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, render_template, request, session as flask_session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import network_manager
import session_manager
from gpio_handler import CoinAcceptor

logger = logging.getLogger("barathrum.portal")

app = Flask(__name__)
app.secret_key = None  # set at startup in main.py once we have a persisted
                        # random key; admin login uses Flask's own signed
                        # session cookie, kept fully separate from the
                        # customer session-token cookie (config.py's two
                        # distinct cookie names/constants).

_coin_acceptor = None  # wired up by main.py after construction, since it
                        # needs a callback into this module


def attach_coin_acceptor(acceptor: CoinAcceptor):
    global _coin_acceptor
    _coin_acceptor = acceptor


# --- identity helpers ----------------------------------------------------

_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def get_client_mac():
    """
    Resolves the requesting device's MAC address via the kernel's
    neighbor table for its source IP. This only works because the box
    itself is the LAN's gateway/DHCP server -- every customer device is
    directly on-link, so `ip neigh` reliably has an entry.

    Returns None if lookup fails (e.g. dev/testing off the real box) --
    callers must handle that rather than assume a MAC is always present.
    """
    client_ip = request.remote_addr
    try:
        result = subprocess.run(
            ["ip", "neigh", "show", client_ip],
            capture_output=True, text=True, check=True,
        )
        match = _MAC_RE.search(result.stdout)
        return match.group(1).lower() if match else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("get_client_mac() failed for %s -- not running on real LAN?", client_ip)
        return None


VOUCHER_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
VOUCHER_CODE_LENGTH = 8
VOUCHER_CODE_MAX_GENERATION_ATTEMPTS = 5


def _generate_unique_voucher_code(minutes):
    """Generates an unused code and inserts it in one step -- collision
    on an 8-char/32-alphabet space (32**8 possibilities) is astronomically
    unlikely, but retried defensively rather than assumed impossible."""
    for _ in range(VOUCHER_CODE_MAX_GENERATION_ATTEMPTS):
        code = "".join(
            secrets.choice(VOUCHER_CODE_ALPHABET) for _ in range(VOUCHER_CODE_LENGTH)
        )
        try:
            db.create_voucher(code, minutes)
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError(
        f"Could not generate a unique voucher code after "
        f"{VOUCHER_CODE_MAX_GENERATION_ATTEMPTS} attempts."
    )


def get_current_session():
    mac = get_client_mac()
    cookie_token = request.cookies.get(config.CUSTOMER_SESSION_COOKIE_NAME)
    if mac is None:
        # Dev/test fallback only -- never expected on the real box, where
        # get_client_mac() always succeeds since every LAN device is
        # directly on-link to the box's own `ip neigh` table.
        # NOTE: an earlier version built this as
        # f"00:...:{request.remote_addr[-2:]}", which is broken -- naive
        # slicing on an IP string like "127.0.0.1" produces ".1", not a
        # valid two-hex-digit MAC octet. Caught during smoke-testing.
        # Hashing the remote_addr instead guarantees a valid, stable
        # synthetic MAC per source IP.
        import hashlib
        digest = hashlib.sha256(request.remote_addr.encode()).hexdigest()
        mac = "02:00:" + ":".join(digest[i:i + 2] for i in range(0, 8, 2))
    return session_manager.resolve_session(mac, cookie_token), mac


def _set_customer_cookie(response, session_token):
    response.set_cookie(
        config.CUSTOMER_SESSION_COOKIE_NAME,
        session_token,
        max_age=config.CUSTOMER_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="Lax",
    )
    return response


# --- customer-facing captive portal (rule #13) --------------------------

@app.route("/")
@app.route("/generate_204")       # Android connectivity check
@app.route("/hotspot-detect.html")  # iOS connectivity check
@app.route("/ncsi.txt")           # Windows connectivity check
def portal_root():
    if not db.is_setup_complete():
        return redirect(url_for("setup_wizard"))

    session, mac = get_current_session()
    response = app.make_response(
        render_template(
            "portal.html",
            session=session,
            ending_soon_threshold=config.SESSION_ENDING_SOON_THRESHOLD_SECONDS,
            arm_window_seconds=config.ARM_ACCEPT_WINDOW_SECONDS,
        )
    )
    return _set_customer_cookie(response, session["session_token"])


@app.route("/api/session/status")
def api_session_status():
    """Polled by the portal page's JS every few seconds to keep the
    live countdown and state (idle/armed/active/paused/ending-soon/
    time's-up) in sync without a full page reload."""
    session, mac = get_current_session()
    armed = _coin_acceptor.is_armed() if _coin_acceptor else False
    armed_by_me = (
        _coin_acceptor is not None
        and _coin_acceptor.get_armed_session_token() == session["session_token"]
    )
    armed_remaining_seconds = (
        _coin_acceptor.get_armed_remaining_seconds() if _coin_acceptor else 0
    )
    return jsonify({
        "status": session["status"],
        "remaining_seconds": session["remaining_seconds"],
        "armed": armed,
        "armed_by_me": armed_by_me,
        "armed_remaining_seconds": armed_remaining_seconds,
        "ending_soon": (
            session["status"] == "active"
            and 0 < session["remaining_seconds"] <= config.SESSION_ENDING_SOON_THRESHOLD_SECONDS
        ),
    })


@app.route("/api/insert-coin/arm", methods=["POST"])
def api_arm_coin_acceptor():
    """Customer taps "Insert Coin" -- arms the relay. Not cosmetic (rule
    customer flow step 4) -- pulses genuinely aren't accepted until this
    fires, and the accept window genuinely does auto-expire (rule #4).

    Passes this request's own session_token to arm() so a real coin
    pulse can be attributed back to the right session. Coin-arming is
    exclusive -- if a different session is already armed, this call is
    rejected outright (see gpio_handler.CoinAcceptor.arm()'s docstring)."""
    if _coin_acceptor is None:
        return jsonify({"error": "Coin acceptor not initialized."}), 503
    session, mac = get_current_session()
    # End Goals #14: gate at ARM time, not pulse time -- refusing to arm means the coin
    # acceptor's relay never enables, so a physical coin is never even accepted by the
    # hardware, and no balance is ever at risk. Checked against remaining_seconds, NOT the raw
    # `status` column: per db.create_session()'s own docstring, a brand-new session is always
    # created with status='active' regardless of balance -- so "already holds a grant" is
    # genuinely remaining_seconds > 0 while not paused. A paused session, or an active session
    # that already has a positive balance (topping up), is never gated; only a genuinely fresh
    # (0 balance) or expired (ran out) session is.
    if (
        session["status"] != "paused"
        and session["remaining_seconds"] <= 0
        and not session_manager.can_grant_new_active_slot()
    ):
        return jsonify({
            "armed": False,
            "error": "Maximum users reached. Try again later.",
        }), 403
    armed = _coin_acceptor.arm(session_token=session["session_token"])
    if not armed:
        return jsonify({
            "armed": False,
            "error": "Coin acceptor is busy.",
        }), 409
    response = jsonify({"armed": True})
    return _set_customer_cookie(response, session["session_token"])


@app.route("/api/insert-coin/done", methods=["POST"])
def api_done_coin_acceptor():
    """Customer taps "Done" in the countdown popup once their coin has
    registered. Idempotent and side-effect-free if called when this
    session isn't the one currently armed (e.g. a stale click after the
    window already auto-expired) -- returns disarmed:false rather than
    an error, since that's not a real failure from the customer's
    perspective."""
    if _coin_acceptor is None:
        return jsonify({"error": "Coin acceptor not initialized."}), 503
    session, mac = get_current_session()
    if _coin_acceptor.get_armed_session_token() != session["session_token"]:
        return jsonify({"disarmed": False})
    _coin_acceptor.disarm()
    return jsonify({"disarmed": True})


@app.route("/api/session/pause", methods=["POST"])
def api_pause_session():
    session, mac = get_current_session()
    try:
        session_manager.pause_session(session["session_token"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "paused"})


@app.route("/api/session/resume", methods=["POST"])
def api_resume_session():
    session, mac = get_current_session()
    try:
        session_manager.resume_session(session["session_token"], requesting_mac_address=mac)
    except (ValueError, PermissionError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "active"})


@app.route("/api/insert-voucher", methods=["POST"])
def api_insert_voucher():
    session, mac = get_current_session()
    voucher_code = request.form.get("voucher_code", "").strip().upper()
    if not voucher_code:
        return jsonify({"error": "Enter a voucher code."}), 400
    try:
        session_manager.handle_voucher_redeem(session["session_token"], voucher_code, mac)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    updated = db.get_session_by_token(session["session_token"])
    return jsonify({"status": updated["status"], "remaining_seconds": updated["remaining_seconds"]})


# --- on-box admin panel (rules #9, #12) ----------------------------------

def admin_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not flask_session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None

    # Lockout check runs BEFORE anything else -- including before even looking at a submitted
    # password on POST -- deliberately, so a locked-out attacker gets zero additional
    # check_password_hash attempts during the window, not just a "you're locked" message tacked
    # onto a failed check.
    locked_until = db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL)
    now = time.time()
    if locked_until and float(locked_until) > now:
        remaining = int(float(locked_until) - now)
        error = f"Too many failed attempts. Try again in {remaining} seconds."
        return render_template("admin_login.html", error=error)

    if request.method == "POST":
        password = request.form.get("password", "")
        stored_hash = db.get_config(db.CFG_ADMIN_PASSWORD_HASH)
        if stored_hash and check_password_hash(stored_hash, password):
            flask_session["admin_authenticated"] = True
            db.set_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS, "0")
            db.set_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL, "")
            return redirect(url_for("admin_home"))

        failed = int(db.get_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS) or 0) + 1
        db.set_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS, str(failed))
        if failed >= config.ADMIN_LOGIN_MAX_FAILED_ATTEMPTS:
            db.set_config(
                db.CFG_ADMIN_LOGIN_LOCKED_UNTIL, str(now + config.ADMIN_LOGIN_LOCKOUT_SECONDS)
            )
            db.set_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS, "0")  # fresh counter after a lockout
            error = (
                f"Too many failed attempts. Try again in "
                f"{config.ADMIN_LOGIN_LOCKOUT_SECONDS} seconds."
            )
        else:
            error = "Incorrect password."

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    flask_session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_login_required
def admin_home():
    active_sessions = [s for s in db.get_all_active_sessions() if s["status"] == "active"]
    return render_template(
        "admin_home.html",
        box_name=db.get_config("box_name", "Barathrum Box"),
        connected_count=len(active_sessions),
        todays_earnings=db.get_todays_earnings_pesos(),
    )


@app.route("/admin/sessions")
@admin_login_required
def admin_sessions():
    sessions = db.get_all_active_sessions()
    return render_template("admin_sessions.html", sessions=sessions)


@app.route("/admin/sessions/<session_token>/revoke", methods=["POST"])
@admin_login_required
def admin_revoke_session(session_token):
    """Manual grant/revoke override for support purposes (rule #9)."""
    session = db.get_session_by_token(session_token)
    if session:
        db.update_remaining(session_token, 0)
        db.set_status(session_token, "expired")
        network_manager.revoke_mac(session["mac_address"])
        logger.info("Admin manually revoked session %s", session_token)
    return redirect(url_for("admin_sessions"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_login_required
def admin_settings():
    if request.method == "POST":
        pesos = request.form.get("pesos_per_pulse")
        minutes = request.form.get("minutes_per_pulse")
        ssid = request.form.get("customer_ssid")
        wifi_password = request.form.get("customer_wifi_password")

        if pesos:
            db.set_config(db.CFG_PESOS_PER_PULSE, pesos)
        if minutes:
            db.set_config(db.CFG_MINUTES_PER_PULSE, minutes)
        if ssid and wifi_password:
            db.set_config(db.CFG_CUSTOMER_SSID, ssid)
            db.set_config(db.CFG_CUSTOMER_WIFI_PASSWORD, wifi_password)
            network_manager.set_customer_wifi(ssid, wifi_password)

        return redirect(url_for("admin_settings"))

    return render_template(
        "admin_settings.html",
        pesos_per_pulse=db.get_config(db.CFG_PESOS_PER_PULSE, config.DEFAULT_PESOS_PER_PULSE),
        minutes_per_pulse=db.get_config(db.CFG_MINUTES_PER_PULSE, config.DEFAULT_MINUTES_PER_PULSE),
        customer_ssid=db.get_config(db.CFG_CUSTOMER_SSID, ""),
        coin_pin=db.get_config(db.CFG_COIN_PIN, config.DEFAULT_COIN_PIN),
        relay_pin=db.get_config(db.CFG_RELAY_PIN, config.DEFAULT_RELAY_PIN),
        arm_ignore_window=config.ARM_IGNORE_WINDOW_SECONDS,
        arm_accept_window=config.ARM_ACCEPT_WINDOW_SECONDS,
    )


@app.route("/admin/vouchers", methods=["GET", "POST"])
@admin_login_required
def admin_vouchers():
    error = None
    new_code = None
    if request.method == "POST":
        minutes_raw = request.form.get("minutes", "")
        try:
            minutes = int(minutes_raw)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            error = "Enter a whole number of minutes greater than 0."
        else:
            new_code = _generate_unique_voucher_code(minutes)

    return render_template(
        "admin_vouchers.html",
        vouchers=db.get_all_vouchers(),
        error=error,
        new_code=new_code,
    )


@app.route("/admin/license", methods=["GET", "POST"])
@admin_login_required
def admin_license():
    """
    End Goals #11 (persistent Bind License, not just the one-time Setup Wizard) + #20 (Unbind
    License, gated by the recovery password -- verified server-side via
    POST /api/box/unbind-license/, since the box never holds the password hash itself). One
    page, toggles between a Bind form and an Unbind form based on whether CFG_LICENSE_KEY is
    currently set -- a box can only ever be bound to one license at a time, so re-binding over
    an existing one is not offered; Unbind first, per this session's PM decision that either
    order is valid but re-binding without unbinding first would leave the old key's grant
    dangling server-side.
    """
    current_key = db.get_config(db.CFG_LICENSE_KEY)
    error = None
    success = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "bind" and not current_key:
            license_key = request.form.get("license_key", "").strip()
            if not license_key:
                error = "Enter a license key."
            else:
                try:
                    resp = requests.post(
                        f"{config.DASHBOARD_API_BASE_URL}/api/box/validate-license/",
                        json={"license_key": license_key},
                        timeout=10,
                    )
                    data = resp.json()
                except (requests.exceptions.RequestException, ValueError):
                    error = "Could not reach the license server. Check your internet connection and try again."
                else:
                    if not data.get("valid"):
                        error = data.get("message", "License key not recognized.")
                    else:
                        db.set_config(db.CFG_LICENSE_KEY, license_key)
                        db.set_config(db.CFG_LICENSE_POINTS, str(data.get("license_points", 0)))
                        db.set_config(db.CFG_LICENSE_NOT_FOUND_STREAK, "0")
                        success = "License bound successfully."
                        current_key = license_key

        elif action == "unbind" and current_key:
            password = request.form.get("password", "")
            try:
                resp = requests.post(
                    f"{config.DASHBOARD_API_BASE_URL}/api/box/unbind-license/",
                    json={"license_key": current_key, "password": password},
                    timeout=10,
                )
                data = resp.json()
            except (requests.exceptions.RequestException, ValueError):
                error = "Could not reach the license server. Check your internet connection and try again."
            else:
                if not data.get("valid"):
                    error = data.get("message", "Incorrect recovery password.")
                else:
                    session_manager._do_local_unbind()
                    success = "License unbound successfully."
                    current_key = ""

    return render_template(
        "admin_license.html",
        current_key=current_key,
        license_points=db.get_config(db.CFG_LICENSE_POINTS, "0"),
        error=error,
        success=success,
    )


# --- Setup Wizard (rule #11) ---------------------------------------------

@app.route("/setup", methods=["GET", "POST"])
def setup_wizard():
    """
    4 screens per rule #11. Kept as a single route with a `step` form
    field for simplicity -- this only ever runs once (or on re-pairing
    after a reflash), so it doesn't need the same polish/state-machine
    rigor as the customer portal.

    NOTE: screen 1's license key is now validated for real against the
    cloud dashboard's POST {DASHBOARD_API_BASE_URL}/api/box/validate-license/
    endpoint (live since Session 63, current contract as of Session 86).
    No CSRF token is sent -- the endpoint is csrf_exempt server-side. A
    network-level failure (timeout, connection error, non-JSON response)
    fails closed: Screen 1 re-renders with a generic error and setup does
    not proceed.
    """
    step = request.form.get("step", "1")

    if request.method == "POST" and step == "1":
        license_key = request.form.get("license_key", "").strip()
        if not license_key:
            return render_template("setup.html", step=1, error="Enter your license key.")
        try:
            resp = requests.post(
                f"{config.DASHBOARD_API_BASE_URL}/api/box/validate-license/",
                json={"license_key": license_key},
                timeout=10,
            )
            data = resp.json()
        except (requests.exceptions.RequestException, ValueError):
            # Fail closed: a network-level failure or a non-JSON response
            # must not be treated as an accepted key. One attempt per form
            # submission -- the operator resubmitting is the retry
            # mechanism, no automatic retry loop here.
            return render_template(
                "setup.html", step=1,
                error="Could not reach the license server. Check your internet connection and try again.",
            )

        if not data.get("valid"):
            return render_template(
                "setup.html", step=1,
                error=data.get("message", "License key not recognized."),
            )

        flask_session["setup_license_key"] = license_key
        # End Goals #17: initial value, free, no extra round-trip -- validate-license's response
        # already includes license_points, so the box stores it immediately on successful bind
        # rather than waiting for the first background sync.
        db.set_config(db.CFG_LICENSE_POINTS, str(data.get("license_points", 0)))
        return render_template("setup.html", step=2)

    if request.method == "POST" and step == "2":
        ssid = request.form.get("customer_ssid", "").strip()
        admin_password = request.form.get("admin_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not ssid or not admin_password:
            return render_template("setup.html", step=2, error="All fields are required.")
        if admin_password != confirm_password:
            return render_template("setup.html", step=2, error="Passwords do not match.")

        db.set_config(db.CFG_LICENSE_KEY, flask_session.get("setup_license_key", ""))
        db.set_config(db.CFG_CUSTOMER_SSID, ssid)
        db.set_config(db.CFG_ADMIN_PASSWORD_HASH, generate_password_hash(admin_password))
        db.set_config(db.CFG_SETUP_COMPLETE, "1")
        return render_template("setup.html", step=3)

    return render_template("setup.html", step=1)


@app.route("/setup/reboot", methods=["POST"])
def setup_reboot():
    """Screen 4: box reboots, drops the temporary setup network, comes
    up on the real customer-facing SSID."""
    ssid = db.get_config(db.CFG_CUSTOMER_SSID)
    wifi_password = request.form.get("customer_wifi_password", "")
    network_manager.set_customer_wifi(ssid, wifi_password)
    # Actual reboot left to the systemd unit / a real `reboot` call in
    # production -- not invoked here directly to keep this testable.
    return jsonify({"status": "rebooting"})
