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
import tempfile
import time
import unittest

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
        resp = self.client.post("/setup", data={"step": "1", "license_key": ""})
        self.assertIn(b"Enter your license key", resp.data)

    def test_full_wizard_flow_completes_and_sets_config(self):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
