"""
Test suite for the Barathrum box agent -- dry-run/simulated only, per this
project's own standard nothing here counts as "confirmed" until it also
runs on real hardware (see STEP_1_dev_handoff_prompt.md).

NOTE ON PROVENANCE: the handoff prompt this file accompanies, and the MPD's
STEP 1 status line, both reference "a full logic test suite" as already
existing and passing. No such file was actually present in the delivered
zip -- this file was written from scratch this session to make that claim
true rather than assumed, exercising the same scenarios the prompt lists
(coin insertion, additive stacking, Pause/Resume mechanics, zero-balance
cutoff, power-loss recovery math, Setup Wizard flow) plus explicit coverage
for the two real bugs found and fixed this session (the never-wired
handle_coin_pulse() callback, and the hostapd open-network WPA-block bug).

Run with:
    BARATHRUM_DRY_RUN=1 python3 -m unittest tests -v
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

os.environ.setdefault("BARATHRUM_DRY_RUN", "1")

import config
import db
import network_manager
import session_manager
from gpio_handler import CoinAcceptor
import portal_app


class BoxAgentTestCase(unittest.TestCase):
    """Base class: fresh SQLite file per test, fresh Flask test client."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="barathrum-test-")
        config.DATA_DIR = self._tmp_dir
        config.DB_PATH = os.path.join(self._tmp_dir, "barathrum.sqlite3")
        db.init_db()
        portal_app.app.config["TESTING"] = True
        portal_app.app.secret_key = "test-secret-key"
        self.client = portal_app.app.test_client()
        # Fresh acceptor per test, dry-run/simulated (no OPi.GPIO on this
        # machine), wired the same way main.py's bootstrap() does.
        self.acceptor = CoinAcceptor(on_coin=self._on_coin)
        portal_app.attach_coin_acceptor(self.acceptor)
        db.set_config(db.CFG_SETUP_COMPLETE, "1")  # skip Setup Wizard redirect by default

    def tearDown(self):
        self.acceptor.disarm()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _on_coin(self):
        """Mirrors main.py's real _on_coin() callback exactly, so these
        tests exercise the actual production wiring, not a shortcut."""
        armed_token = self.acceptor.get_armed_session_token()
        if armed_token is None:
            return
        session_manager.handle_coin_pulse(armed_token)


class CoinInsertionTests(BoxAgentTestCase):
    def test_arm_then_pulse_adds_balance_and_grants_mac(self):
        resp = self.client.post("/api/insert-coin/arm")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.acceptor.is_armed())

        # Bypass the 1.5s ignore window deliberately (matches real coin
        # timing, not a workaround) -- back-date arm start.
        self.acceptor._arm_started_at -= 2.0
        self.acceptor.simulate_pulse()

        status = self.client.get("/api/session/status").get_json()
        pesos, minutes = session_manager._get_rate()
        self.assertEqual(status["remaining_seconds"], minutes * 60)
        self.assertEqual(status["status"], "active")

    def test_pulse_within_ignore_window_is_dropped(self):
        self.client.post("/api/insert-coin/arm")
        # No back-dating this time -- pulse arrives inside the 1.5s
        # power-up phantom-pulse suppression window (rule #4).
        self.acceptor.simulate_pulse()
        status = self.client.get("/api/session/status").get_json()
        self.assertEqual(status["remaining_seconds"], 0)

    def test_pulse_while_nothing_armed_is_ignored_not_crashed(self):
        # No arm() call at all -- the fixed _on_coin() must not raise or
        # attribute the pulse to some stale/previous session.
        self.acceptor.simulate_pulse()
        # No assertion possible on "which session" since none was armed;
        # this test's real point is that simulate_pulse() doesn't raise.

    def test_additive_stacking_multiple_coins_same_arm_window(self):
        self.client.post("/api/insert-coin/arm")
        self.acceptor._arm_started_at -= 2.0
        # Spaced out past DEBOUNCE_SECONDS -- real coin pulses arrive with
        # real gaps between them; firing simulate_pulse() with zero delay
        # is what the debounce filter is specifically supposed to reject
        # as noise, so a realistic gap here is the correct thing to test.
        self.acceptor.simulate_pulse()
        time.sleep(config.DEBOUNCE_SECONDS * 2)
        self.acceptor.simulate_pulse()
        time.sleep(config.DEBOUNCE_SECONDS * 2)
        self.acceptor.simulate_pulse()
        status = self.client.get("/api/session/status").get_json()
        pesos, minutes = session_manager._get_rate()
        self.assertEqual(status["remaining_seconds"], 3 * minutes * 60)

    def test_arm_race_most_recent_armer_receives_the_pulse(self):
        """
        Documented, accepted behavior (see gpio_handler.CoinAcceptor.arm()
        docstring): two devices both tap "Insert Coin" within the same
        window -- the pulse goes to whoever armed MOST RECENTLY, not the
        first armer. This test pins that behavior down explicitly rather
        than leaving it unverified.
        """
        session_a, mac_a = session_manager.resolve_session("aa:aa:aa:aa:aa:aa"), "aa:aa:aa:aa:aa:aa"
        session_b, mac_b = session_manager.resolve_session("bb:bb:bb:bb:bb:bb"), "bb:bb:bb:bb:bb:bb"

        self.acceptor.arm(session_token=session_a["session_token"])
        self.acceptor.arm(session_token=session_b["session_token"])  # B arms second -- wins
        self.acceptor._arm_started_at -= 2.0
        self.acceptor.simulate_pulse()

        refreshed_a = db.get_session_by_token(session_a["session_token"])
        refreshed_b = db.get_session_by_token(session_b["session_token"])
        self.assertEqual(refreshed_a["remaining_seconds"], 0)
        self.assertGreater(refreshed_b["remaining_seconds"], 0)


class ZeroBalanceCutoffTests(BoxAgentTestCase):
    def test_tick_to_zero_revokes_immediately_no_grace_period(self):
        session = session_manager.resolve_session("cc:cc:cc:cc:cc:cc")
        session_manager.handle_coin_pulse(session["session_token"])  # gives it a balance
        session = db.get_session_by_token(session["session_token"])
        self.assertEqual(session["status"], "active")

        session_manager.tick_active_sessions(elapsed_seconds=session["remaining_seconds"] + 1)

        expired = db.get_session_by_token(session["session_token"])
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["remaining_seconds"], 0)


class PauseResumeTests(BoxAgentTestCase):
    def setUp(self):
        super().setUp()
        self.session = session_manager.resolve_session("dd:dd:dd:dd:dd:dd")
        session_manager.handle_coin_pulse(self.session["session_token"])
        self.session = db.get_session_by_token(self.session["session_token"])

    def test_pause_freezes_balance_and_cuts_access_not_disconnect(self):
        remaining_before = self.session["remaining_seconds"]
        session_manager.pause_session(self.session["session_token"])
        paused = db.get_session_by_token(self.session["session_token"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["remaining_seconds"], remaining_before)

        # Freeze is real: ticking the countdown loop must not touch a
        # paused session's balance.
        session_manager.tick_active_sessions(elapsed_seconds=30)
        still_paused = db.get_session_by_token(self.session["session_token"])
        self.assertEqual(still_paused["remaining_seconds"], remaining_before)

    def test_coin_while_paused_tops_up_without_forcing_resume(self):
        session_manager.pause_session(self.session["session_token"])
        remaining_before = db.get_session_by_token(self.session["session_token"])["remaining_seconds"]
        session_manager.handle_coin_pulse(self.session["session_token"])
        after = db.get_session_by_token(self.session["session_token"])
        self.assertGreater(after["remaining_seconds"], remaining_before)
        self.assertEqual(after["status"], "paused")  # still paused, not forced active

    def test_resume_requires_same_mac(self):
        session_manager.pause_session(self.session["session_token"])
        with self.assertRaises(PermissionError):
            session_manager.resume_session(
                self.session["session_token"], requesting_mac_address="ff:ff:ff:ff:ff:ff"
            )
        session_manager.resume_session(
            self.session["session_token"], requesting_mac_address="dd:dd:dd:dd:dd:dd"
        )
        resumed = db.get_session_by_token(self.session["session_token"])
        self.assertEqual(resumed["status"], "active")

    def test_multiple_pause_resume_cycles_allowed(self):
        token = self.session["session_token"]
        for _ in range(3):
            session_manager.pause_session(token)
            session_manager.resume_session(token, requesting_mac_address="dd:dd:dd:dd:dd:dd")
        final = db.get_session_by_token(token)
        self.assertEqual(final["status"], "active")

    def test_30_day_abandonment_forfeits_balance(self):
        token = self.session["session_token"]
        session_manager.pause_session(token)
        # Backdate paused_at past the 30-day threshold rather than
        # sleeping in the test.
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET paused_at = ? WHERE session_token = ?",
                (time.time() - (config.PAUSE_ABANDONMENT_EXPIRY_DAYS * 86400) - 1, token),
            )
        session_manager.check_pause_abandonment()
        forfeited = db.get_session_by_token(token)
        self.assertEqual(forfeited["status"], "expired")
        self.assertEqual(forfeited["remaining_seconds"], 0)

    def test_abandonment_clock_resets_on_each_resume_then_pause(self):
        token = self.session["session_token"]
        session_manager.pause_session(token)
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET paused_at = ? WHERE session_token = ?",
                (time.time() - (config.PAUSE_ABANDONMENT_EXPIRY_DAYS * 86400) + 3600, token),
            )
        session_manager.resume_session(token, requesting_mac_address="dd:dd:dd:dd:dd:dd")
        session_manager.pause_session(token)  # fresh paused_at, per rule
        session_manager.check_pause_abandonment()
        still_alive = db.get_session_by_token(token)
        self.assertEqual(still_alive["status"], "paused")


class PowerLossRecoveryTests(BoxAgentTestCase):
    def test_recovery_decrements_by_real_elapsed_time_not_free_resume(self):
        session = session_manager.resolve_session("ee:ee:ee:ee:ee:ee")
        session_manager.handle_coin_pulse(session["session_token"])  # e.g. 300s
        token = session["session_token"]
        remaining_before = db.get_session_by_token(token)["remaining_seconds"]

        # Simulate 60s of real downtime by back-dating last_updated_at.
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_updated_at = ? WHERE session_token = ?",
                (time.time() - 60, token),
            )
        session_manager.recover_sessions_on_boot()
        recovered = db.get_session_by_token(token)
        self.assertAlmostEqual(
            recovered["remaining_seconds"], remaining_before - 60, delta=2
        )
        self.assertEqual(recovered["status"], "active")

    def test_recovery_expires_session_that_ran_out_while_down(self):
        session = session_manager.resolve_session("11:11:11:11:11:11")
        session_manager.handle_coin_pulse(session["session_token"])
        token = session["session_token"]
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_updated_at = ? WHERE session_token = ?",
                (time.time() - 10_000_000, token),  # way more than any balance
            )
        session_manager.recover_sessions_on_boot()
        expired = db.get_session_by_token(token)
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["remaining_seconds"], 0)

    def test_paused_session_is_not_decremented_across_reboot(self):
        session = session_manager.resolve_session("22:22:22:22:22:22")
        session_manager.handle_coin_pulse(session["session_token"])
        token = session["session_token"]
        session_manager.pause_session(token)
        remaining_before = db.get_session_by_token(token)["remaining_seconds"]
        with db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_updated_at = ? WHERE session_token = ?",
                (time.time() - 999999, token),
            )
        session_manager.recover_sessions_on_boot()
        after = db.get_session_by_token(token)
        self.assertEqual(after["remaining_seconds"], remaining_before)
        self.assertEqual(after["status"], "paused")


class SetupWizardTests(BoxAgentTestCase):
    def setUp(self):
        super().setUp()
        db.set_config(db.CFG_SETUP_COMPLETE, "0")  # override: wizard not yet done

    def test_root_redirects_to_setup_when_incomplete(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/setup", resp.headers["Location"])

    def test_screen1_rejects_empty_license_key(self):
        # No HTTP call should happen at all -- caught client-side before
        # requests.post is ever reached.
        with patch("portal_app.requests.post") as mock_post:
            resp = self.client.post("/setup", data={"step": "1", "license_key": ""})
            mock_post.assert_not_called()
        self.assertIn(b"Enter your license key", resp.data)

    def test_screen1_accepts_real_valid_license_key(self):
        with patch("portal_app.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "valid": True, "message": "License validated.",
            }
            resp = self.client.post("/setup", data={"step": "1", "license_key": "TESTKEY123"})
        self.assertEqual(resp.status_code, 200)
        mock_post.assert_called_once()
        called_url = mock_post.call_args.args[0]
        self.assertTrue(called_url.endswith("/api/box/validate-license/"))
        self.assertEqual(mock_post.call_args.kwargs["json"], {"license_key": "TESTKEY123"})
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 10)

    def test_screen1_rejects_nonexistent_license_key(self):
        with patch("portal_app.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "valid": False, "message": "License key not recognized.",
            }
            resp = self.client.post("/setup", data={"step": "1", "license_key": "NOSUCHKEY"})
        self.assertIn(b"License key not recognized.", resp.data)

    def test_screen1_network_failure_shows_generic_error_and_does_not_proceed(self):
        with patch("portal_app.requests.post") as mock_post:
            mock_post.side_effect = requests.exceptions.ConnectionError("no route to host")
            resp = self.client.post("/setup", data={"step": "1", "license_key": "TESTKEY123"})
        self.assertIn(b"Could not reach the license server", resp.data)
        with self.client.session_transaction() as sess:
            self.assertNotIn("setup_license_key", sess)

    def test_full_wizard_flow_completes_and_sets_config(self):
        with patch("portal_app.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "valid": True, "message": "License validated.",
            }
            resp = self.client.post("/setup", data={"step": "1", "license_key": "TESTKEY123"})
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            "/setup",
            data={
                "step": "2",
                "customer_ssid": "MyShopWiFi",
                "admin_password": "correct horse battery",
                "confirm_password": "correct horse battery",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(db.is_setup_complete())
        self.assertEqual(db.get_config(db.CFG_CUSTOMER_SSID), "MyShopWiFi")
        self.assertEqual(db.get_config(db.CFG_LICENSE_KEY), "TESTKEY123")

    def test_mismatched_passwords_rejected(self):
        with patch("portal_app.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "valid": True, "message": "License validated.",
            }
            self.client.post("/setup", data={"step": "1", "license_key": "TESTKEY123"})
        resp = self.client.post(
            "/setup",
            data={
                "step": "2",
                "customer_ssid": "MyShopWiFi",
                "admin_password": "aaa",
                "confirm_password": "bbb",
            },
        )
        self.assertIn(b"do not match", resp.data)
        self.assertFalse(db.is_setup_complete())


class HostapdConfigTests(unittest.TestCase):
    """
    Regression coverage for the bug fixed this session: an empty
    passphrase (Setup Wizard's temporary open network) must omit the
    wpa* lines entirely, not render wpa=2 with an empty
    wpa_passphrase= (which hostapd itself rejects at startup).
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="barathrum-hostapd-test-")
        config.DATA_DIR = self._tmp_dir

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_open_network_omits_wpa_block(self):
        network_manager.DRY_RUN = True
        # render_hostapd_config logs the rendered content via the module
        # logger in DRY_RUN mode rather than returning it -- capture by
        # monkeypatching the module logger briefly.
        captured = {}
        original_info = network_manager.logger.info

        def _capture(msg, *args):
            captured["text"] = msg % args if args else msg

        network_manager.logger.info = _capture
        try:
            network_manager.render_hostapd_config("Barathrum-Setup-AB12", passphrase="")
        finally:
            network_manager.logger.info = original_info

        rendered = captured["text"]
        self.assertNotIn("wpa=2", rendered)
        self.assertNotIn("wpa_passphrase=", rendered)  # the directive line, not comment prose
        self.assertIn("ssid=Barathrum-Setup-AB12", rendered)

    def test_secured_network_includes_wpa_block(self):
        network_manager.DRY_RUN = True
        captured = {}
        original_info = network_manager.logger.info

        def _capture(msg, *args):
            captured["text"] = msg % args if args else msg

        network_manager.logger.info = _capture
        try:
            network_manager.render_hostapd_config("MyShopWiFi", passphrase="supersecret123")
        finally:
            network_manager.logger.info = original_info

        rendered = captured["text"]
        self.assertIn("wpa=2", rendered)
        self.assertIn("wpa_passphrase=supersecret123", rendered)


class WifiModeTests(unittest.TestCase):
    """
    Coverage for config.WIFI_MODE ("onboard_hostapd" default vs.
    "external_ap"). Uses the same DRY_RUN logger-capture pattern as
    HostapdConfigTests above.
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="barathrum-wifimode-test-")
        config.DATA_DIR = self._tmp_dir
        self._original_wifi_mode = config.WIFI_MODE
        network_manager.DRY_RUN = True

    def tearDown(self):
        config.WIFI_MODE = self._original_wifi_mode
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _capture_logs(self):
        captured = []
        original_info = network_manager.logger.info

        def _capture(msg, *args):
            captured.append(msg % args if args else msg)
            original_info(msg, *args)

        network_manager.logger.info = _capture
        return captured, original_info

    # --- default mode: behavior must be unchanged -----------------------

    def test_default_wifi_mode_is_onboard_hostapd(self):
        self.assertEqual(config.WIFI_MODE, "onboard_hostapd")

    def test_onboard_hostapd_restart_still_restarts_hostapd(self):
        config.WIFI_MODE = "onboard_hostapd"
        captured, original_info = self._capture_logs()
        try:
            network_manager.restart_network_services()
        finally:
            network_manager.logger.info = original_info
        joined = " ".join(captured)
        self.assertIn("would run: systemctl restart hostapd", joined)
        self.assertIn("would run: systemctl restart dnsmasq", joined)

    # --- external_ap mode -------------------------------------------------

    def test_external_ap_restart_skips_hostapd_but_restarts_dnsmasq(self):
        config.WIFI_MODE = "external_ap"
        captured, original_info = self._capture_logs()
        try:
            network_manager.restart_network_services()
        finally:
            network_manager.logger.info = original_info
        joined = " ".join(captured)
        self.assertNotIn("hostapd", joined)
        self.assertIn("would run: systemctl restart dnsmasq", joined)

    def test_external_ap_set_customer_wifi_skips_hostapd_render(self):
        config.WIFI_MODE = "external_ap"
        original_render = network_manager.render_hostapd_config
        was_called = {"flag": False}

        def _spy(*args, **kwargs):
            was_called["flag"] = True
            return original_render(*args, **kwargs)

        network_manager.render_hostapd_config = _spy
        captured, original_info = self._capture_logs()
        try:
            network_manager.set_customer_wifi("SomeSSID", "somepassphrase")
        finally:
            network_manager.render_hostapd_config = original_render
            network_manager.logger.info = original_info

        self.assertFalse(was_called["flag"])
        self.assertTrue(any("skipping hostapd config" in line for line in captured))

    def test_external_ap_broadcast_setup_network_skips_hostapd_render(self):
        config.WIFI_MODE = "external_ap"
        original_render = network_manager.render_hostapd_config
        was_called = {"flag": False}

        def _spy(*args, **kwargs):
            was_called["flag"] = True
            return original_render(*args, **kwargs)

        network_manager.render_hostapd_config = _spy
        captured, original_info = self._capture_logs()
        try:
            network_manager.broadcast_setup_network("AB12")
        finally:
            network_manager.render_hostapd_config = original_render
            network_manager.logger.info = original_info

        self.assertFalse(was_called["flag"])
        self.assertTrue(any("skipping setup-network hostapd broadcast" in line for line in captured))

    # --- firewall/grant/revoke: must behave identically in both modes ----

    def _firewall_smoke_test(self):
        """Runs apply_base_firewall_policy + grant_mac + revoke_mac and
        returns the list of commands that would have run, via DRY_RUN
        log capture -- used identically under both WIFI_MODE values."""
        captured = []
        original_info = network_manager.logger.info

        def _capture(msg, *args):
            captured.append(msg % args if args else msg)

        network_manager.logger.info = _capture
        try:
            network_manager.apply_base_firewall_policy()
            network_manager.grant_mac("aa:bb:cc:dd:ee:ff")
            network_manager.revoke_mac("aa:bb:cc:dd:ee:ff")
        finally:
            network_manager.logger.info = original_info
        return captured

    def test_firewall_functions_identical_in_onboard_hostapd_mode(self):
        config.WIFI_MODE = "onboard_hostapd"
        captured = self._firewall_smoke_test()
        self.assertTrue(any("Base firewall policy applied" in line for line in captured))
        self.assertTrue(any("Access GRANTED" in line for line in captured))
        self.assertTrue(any("Access REVOKED" in line for line in captured))

    def test_firewall_functions_identical_in_external_ap_mode(self):
        config.WIFI_MODE = "external_ap"
        captured = self._firewall_smoke_test()
        self.assertTrue(any("Base firewall policy applied" in line for line in captured))
        self.assertTrue(any("Access GRANTED" in line for line in captured))
        self.assertTrue(any("Access REVOKED" in line for line in captured))


class AdminPanelTests(BoxAgentTestCase):
    def setUp(self):
        super().setUp()
        from werkzeug.security import generate_password_hash
        db.set_config(db.CFG_ADMIN_PASSWORD_HASH, generate_password_hash("adminpass123"))

    def test_admin_routes_require_login(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login", resp.headers["Location"])

    def test_wrong_password_rejected(self):
        resp = self.client.post("/admin/login", data={"password": "wrong"})
        self.assertIn(b"Incorrect password", resp.data)

    def test_correct_password_grants_access(self):
        resp = self.client.post(
            "/admin/login", data={"password": "adminpass123"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)

    def test_manual_revoke_expires_session(self):
        self.test_correct_password_grants_access()
        session = session_manager.resolve_session("33:33:33:33:33:33")
        session_manager.handle_coin_pulse(session["session_token"])
        resp = self.client.post(f"/admin/sessions/{session['session_token']}/revoke")
        self.assertEqual(resp.status_code, 302)
        revoked = db.get_session_by_token(session["session_token"])
        self.assertEqual(revoked["status"], "expired")
        self.assertEqual(revoked["remaining_seconds"], 0)


class AdminLoginLockoutTests(BoxAgentTestCase):
    """STEP 1 tracker row 25 -- brute-force lockout on the local admin login."""

    def setUp(self):
        super().setUp()
        from werkzeug.security import generate_password_hash
        db.set_config(db.CFG_ADMIN_PASSWORD_HASH, generate_password_hash("adminpass123"))

    def _fail_login(self):
        return self.client.post("/admin/login", data={"password": "wrong"})

    def test_correct_password_first_try_counter_stays_zero(self):
        resp = self.client.post(
            "/admin/login", data={"password": "adminpass123"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS), "0")

    def test_single_wrong_password_increments_counter_no_lockout_yet(self):
        resp = self._fail_login()
        self.assertIn(b"Incorrect password", resp.data)
        self.assertNotIn(b"Too many failed attempts", resp.data)
        self.assertEqual(db.get_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS), "1")
        self.assertFalse(db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL))

    def test_max_failed_attempts_triggers_lockout_and_resets_counter(self):
        for _ in range(config.ADMIN_LOGIN_MAX_FAILED_ATTEMPTS - 1):
            resp = self._fail_login()
            self.assertIn(b"Incorrect password", resp.data)

        # the attempt that crosses the threshold
        resp = self._fail_login()
        self.assertIn(b"Too many failed attempts", resp.data)
        self.assertEqual(db.get_config(db.CFG_ADMIN_LOGIN_FAILED_ATTEMPTS), "0")
        locked_until = db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL)
        self.assertTrue(locked_until)
        self.assertGreater(float(locked_until), time.time())

    def test_correct_password_rejected_while_locked(self):
        """Confirms the lockout check runs BEFORE the password check, not after --
        the right password must not succeed during an active lockout window."""
        for _ in range(config.ADMIN_LOGIN_MAX_FAILED_ATTEMPTS):
            self._fail_login()

        resp = self.client.post("/admin/login", data={"password": "adminpass123"})
        self.assertIn(b"Too many failed attempts", resp.data)
        # still not authenticated -- confirm no session was granted
        home_resp = self.client.get("/admin")
        self.assertEqual(home_resp.status_code, 302)
        self.assertIn("/admin/login", home_resp.headers["Location"])

    def test_login_works_again_after_lockout_window_passes(self):
        for _ in range(config.ADMIN_LOGIN_MAX_FAILED_ATTEMPTS):
            self._fail_login()
        self.assertTrue(db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL))

        # Backdate CFG_ADMIN_LOGIN_LOCKED_UNTIL into the past rather than sleeping in the test,
        # same pattern SessionPauseResumeTests.test_30_day_abandonment_forfeits_balance uses for
        # paused_at.
        db.set_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL, str(time.time() - 1))

        resp = self.client.post(
            "/admin/login", data={"password": "adminpass123"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        home_resp = self.client.get("/admin")
        self.assertEqual(home_resp.status_code, 200)

    def test_lockout_state_survives_a_fresh_app_context_reset(self):
        """Confirms the counter is read from the real config table, not an in-memory
        variable that would silently reset -- the whole point of this task."""
        for _ in range(config.ADMIN_LOGIN_MAX_FAILED_ATTEMPTS):
            self._fail_login()
        locked_until_before = db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL)
        self.assertTrue(locked_until_before)

        # Fresh CoinAcceptor + fresh Flask test client, same DB file -- mirrors what a real
        # process restart looks like from the app's own perspective, without tearing down the
        # SQLite file this test's config lives in.
        self.acceptor.disarm()
        self.acceptor = CoinAcceptor(on_coin=self._on_coin)
        portal_app.attach_coin_acceptor(self.acceptor)
        self.client = portal_app.app.test_client()

        resp = self.client.post("/admin/login", data={"password": "adminpass123"})
        self.assertIn(b"Too many failed attempts", resp.data)
        self.assertEqual(db.get_config(db.CFG_ADMIN_LOGIN_LOCKED_UNTIL), locked_until_before)


class InstallScriptTagPinningTests(unittest.TestCase):
    """Session 88 + Session 90 (Security Findings #14/#15, tracker row 27):
    install.sh must pin step_clone_or_update_repo() to the immutable
    INSTALL_COMMIT_SHA -- never a mutable tag name, never a branch -- and
    must verify an already-cloned directory's origin remote before any
    fetch/reset. These tests exercise the real function against a real
    local throwaway git remote -- not a mock of git commands -- so they
    genuinely prove a force-moved tag or a repointed origin can't silently
    change what gets installed."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        with open(cls.script_path) as f:
            cls.script_source = f.read()

    def _run(self, *args, cwd=None):
        return subprocess.run(list(args), check=True, capture_output=True, text=True, cwd=cwd)

    def _rev_parse(self, repo_dir, ref):
        return self._run("git", "-C", repo_dir, "rev-parse", ref).stdout.strip()

    def _no_main_source(self):
        # Strip the trailing `main "$@"` invocation so the script's real
        # functions and Config section can be sourced without triggering
        # the apt/systemd install steps (which need root and a real box).
        lines = self.script_source.splitlines()
        return "\n".join(l for l in lines if l.strip() != 'main "$@"')

    def _sourced_script_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tf:
            tf.write(self._no_main_source())
            return tf.name

    def _run_clone_step(self, repo_url, install_dir, install_tag="v1.0.0", install_commit_sha=None):
        sourced_path = self._sourced_script_path()
        try:
            bash_cmd = f'source "{sourced_path}"; REPO_URL="{repo_url}"; INSTALL_DIR="{install_dir}"; INSTALL_TAG="{install_tag}"; '
            if install_commit_sha is not None:
                bash_cmd += f'INSTALL_COMMIT_SHA="{install_commit_sha}"; '
            bash_cmd += "step_clone_or_update_repo"
            return subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
        finally:
            os.unlink(sourced_path)

    def _run_require_real_commit_pin(self, install_commit_sha=None):
        sourced_path = self._sourced_script_path()
        try:
            bash_cmd = f'source "{sourced_path}"; '
            if install_commit_sha is not None:
                bash_cmd += f'INSTALL_COMMIT_SHA="{install_commit_sha}"; '
            bash_cmd += "require_real_commit_pin"
            return subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
        finally:
            os.unlink(sourced_path)

    def _make_remote_with_tagged_and_untested_commits(self, tmp, remote_name="remote.git"):
        """Returns (remote_path, work_dir, tagged_commit_sha)."""
        remote = os.path.join(tmp, remote_name)
        self._run("git", "init", "--bare", "-b", "main", remote)
        work = os.path.join(tmp, remote_name.replace(".git", "") + "-work")
        self._run("git", "clone", remote, work)
        self._run("git", "-C", work, "config", "user.email", "t@example.com")
        self._run("git", "-C", work, "config", "user.name", "Test")
        with open(os.path.join(work, "f.txt"), "w") as f:
            f.write("v1 -- tagged release\n")
        self._run("git", "-C", work, "add", "f.txt")
        self._run("git", "-C", work, "commit", "-m", "v1")
        self._run("git", "-C", work, "tag", "v1.0.0")
        tagged_sha = self._rev_parse(work, "v1.0.0")
        with open(os.path.join(work, "f.txt"), "w") as f:
            f.write("v2 -- untested, still on main only\n")
        self._run("git", "-C", work, "add", "f.txt")
        self._run("git", "-C", work, "commit", "-m", "v2 untested")
        self._run("git", "-C", work, "push", "origin", "main")
        self._run("git", "-C", work, "push", "origin", "v1.0.0")
        return remote, work, tagged_sha

    # -- static source checks -------------------------------------------

    def test_default_branch_detection_removed(self):
        self.assertNotIn("default_branch", self.script_source)

    def test_install_tag_constant_present(self):
        self.assertIn('INSTALL_TAG="v1.0.0"', self.script_source)

    def test_install_commit_sha_placeholder_shipped(self):
        self.assertIn('INSTALL_COMMIT_SHA="REPLACE_AT_RELEASE_CUT"', self.script_source)

    # -- test 1/2: require_real_commit_pin guard -------------------------

    def test_1_guard_fires_on_shipped_placeholder(self):
        result = self._run_require_real_commit_pin()  # leave at real shipped default
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REPLACE_AT_RELEASE_CUT", result.stderr)
        self.assertIn("RELEASE RITUAL", result.stderr)

    def test_2_guard_passes_for_real_looking_sha(self):
        result = self._run_require_real_commit_pin(install_commit_sha="a" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip(), "")

    def test_guard_rejects_too_short_hex_string(self):
        bad_value = "a" * 10
        result = self._run_require_real_commit_pin(install_commit_sha=bad_value)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(bad_value, result.stderr)
        self.assertIn("40-character", result.stderr)

    def test_guard_rejects_too_long_hex_string(self):
        bad_value = "a" * 50
        result = self._run_require_real_commit_pin(install_commit_sha=bad_value)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(bad_value, result.stderr)
        self.assertIn("40-character", result.stderr)

    def test_guard_rejects_uppercase_hex(self):
        bad_value = "A" * 40
        result = self._run_require_real_commit_pin(install_commit_sha=bad_value)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(bad_value, result.stderr)

    def test_guard_rejects_tag_name_pasted_instead_of_sha(self):
        result = self._run_require_real_commit_pin(install_commit_sha="v1.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v1.0.0", result.stderr)
        self.assertIn("git rev-parse", result.stderr)

    def test_guard_rejects_trailing_whitespace_copy_paste_artifact(self):
        bad_value = ("a" * 40) + "\n"
        result = self._run_require_real_commit_pin(install_commit_sha=bad_value)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("40-character", result.stderr)

    # -- test 3/4: SHA pinning behaves like tag pinning did --------------

    def test_3_fresh_clone_checks_out_pinned_sha_not_untested_main_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _work, tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            install_dir = os.path.join(tmp, "installed")
            result = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(install_dir, "f.txt")) as f:
                content = f.read()
            self.assertIn("v1 -- tagged release", content)
            self.assertNotIn("untested", content)

    def test_4_already_cloned_directory_resets_to_pinned_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _work, tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            install_dir = os.path.join(tmp, "installed")
            # Simulate a box already checked out to the untested main HEAD.
            self._run("git", "clone", remote, install_dir)
            self._run("git", "-C", install_dir, "checkout", "main")
            result = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(install_dir, "f.txt")) as f:
                content = f.read()
            self.assertIn("v1 -- tagged release", content)
            self.assertNotIn("untested", content)

    # -- test 5: the money test -- force-moving the tag changes nothing --

    def test_5_force_moved_tag_does_not_change_what_gets_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, work, tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            # Simulate a compromised release: push a third commit and force-move
            # v1.0.0 to point at it, exactly as an attacker with repo/release
            # write access could.
            with open(os.path.join(work, "f.txt"), "w") as f:
                f.write("v3 -- MALICIOUS, tag force-moved here\n")
            self._run("git", "-C", work, "add", "f.txt")
            self._run("git", "-C", work, "commit", "-m", "v3 malicious")
            self._run("git", "-C", work, "push", "origin", "main")
            self._run("git", "-C", work, "tag", "-f", "v1.0.0")
            self._run("git", "-C", work, "push", "origin", "v1.0.0", "--force")

            # Confirm the tag really did move on the remote, so this test would
            # actually catch a regression rather than passing vacuously.
            moved_sha = self._rev_parse(work, "v1.0.0")
            self.assertNotEqual(moved_sha, tagged_sha)

            install_dir = os.path.join(tmp, "installed")
            # Pin to the ORIGINAL captured SHA -- not re-resolved from the tag.
            result = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(install_dir, "f.txt")) as f:
                content = f.read()
            self.assertIn("v1 -- tagged release", content)
            self.assertNotIn("MALICIOUS", content)

    # -- test 6: idempotency ----------------------------------------------

    def test_6_rerun_against_already_pinned_directory_is_idempotent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _work, tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            install_dir = os.path.join(tmp, "installed")
            first = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(second.returncode, 0, second.stderr)
            with open(os.path.join(install_dir, "f.txt")) as f:
                content = f.read()
            self.assertIn("v1 -- tagged release", content)

    # -- test 7: nonexistent SHA fails loudly ------------------------------

    def test_7_nonexistent_sha_fails_loudly_naming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _work, _tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            install_dir = os.path.join(tmp, "installed")
            bogus_sha = "f" * 40
            result = self._run_clone_step(remote, install_dir, install_commit_sha=bogus_sha)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(bogus_sha, result.stderr)
            self.assertIn("git ls-remote", result.stderr)

    # -- test 8/9: origin verification --------------------------------------

    def test_8_origin_mismatch_rejected_before_any_fetch_or_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_real, _work_real, tagged_sha = self._make_remote_with_tagged_and_untested_commits(
                tmp, remote_name="remote-real.git"
            )
            remote_other, _work_other, _other_sha = self._make_remote_with_tagged_and_untested_commits(
                tmp, remote_name="remote-other.git"
            )
            install_dir = os.path.join(tmp, "installed")
            # Box was cloned from (or repointed to, by a prior compromise) the
            # WRONG remote.
            self._run("git", "clone", remote_other, install_dir)
            with open(os.path.join(install_dir, "f.txt")) as f:
                before_content = f.read()

            result = self._run_clone_step(remote_real, install_dir, install_commit_sha=tagged_sha)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(remote_other, result.stderr)
            self.assertIn(remote_real, result.stderr)
            # No fetch/reset happened -- working tree is byte-identical to
            # before the call, proving the check runs before any git network
            # operation, not merely that a later step also happened to fail.
            with open(os.path.join(install_dir, "f.txt")) as f:
                after_content = f.read()
            self.assertEqual(before_content, after_content)

    def test_9_origin_match_still_succeeds_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, _work, tagged_sha = self._make_remote_with_tagged_and_untested_commits(tmp)
            install_dir = os.path.join(tmp, "installed")
            self._run("git", "clone", remote, install_dir)
            self._run("git", "-C", install_dir, "checkout", "main")
            result = self._run_clone_step(remote, install_dir, install_commit_sha=tagged_sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(install_dir, "f.txt")) as f:
                content = f.read()
            self.assertIn("v1 -- tagged release", content)
            self.assertNotIn("untested", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
