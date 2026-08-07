"""
Coin acceptor GPIO handling (STEP 1 rules #3, #4).

Uses OPi.GPIO (sysfs-based), NOT RPi.GPIO/gpiozero -- MPD is explicit that
Orange Pi boards don't share Raspberry Pi's GPIO library, and that pin
numbering is reportedly mirrored/flipped 180 degrees on the Orange Pi One
specifically relative to the Orange Pi PC. Pin numbers in config.py MUST
be re-verified against the real board before wiring anything -- nothing
here can substitute for that physical check.

Falls back to a SIMULATED mode (no real GPIO access) when OPi.GPIO isn't
importable, e.g. when this code is being reviewed/tested on a regular
dev machine rather than the actual Orange Pi. This lets the rest of the
agent (session logic, portal, admin panel) be built and smoke-tested
without the physical board -- but per the MPD's own verification
standard, none of this is "confirmed" until it's actually run on real
hardware with a real coin acceptor wired up.
"""

import logging
import threading
import time

import config

logger = logging.getLogger("barathrum.gpio")

try:
    import OPi.GPIO as GPIO  # type: ignore
    _HARDWARE_AVAILABLE = True
except ImportError:
    GPIO = None
    _HARDWARE_AVAILABLE = False
    logger.warning(
        "OPi.GPIO not importable -- running in SIMULATED coin-pulse mode. "
        "This is expected on a dev machine, NOT expected on the real box."
    )


class CoinAcceptor:
    """
    Tracks the arm/disarm lifecycle and counts debounced coin pulses while
    armed. Call `arm()` when the customer taps "Insert Coin"; pulses before
    the ignore window or after the accept window (with no activity) are
    not counted, per rule #4.

    `on_coin` is called once per debounced pulse while armed, with no
    arguments -- the caller (session_manager) is responsible for deciding
    what a pulse is worth in minutes.
    """

    def __init__(self, coin_pin=None, relay_pin=None, active_low=None, on_coin=None):
        self.coin_pin = coin_pin if coin_pin is not None else config.DEFAULT_COIN_PIN
        self.relay_pin = relay_pin if relay_pin is not None else config.DEFAULT_RELAY_PIN
        self.active_low = (
            active_low if active_low is not None else config.DEFAULT_PULSE_ACTIVE_LOW
        )
        self.on_coin = on_coin or (lambda: None)

        self._armed = False
        self._armed_session_token = None
        self._arm_started_at = None
        self._last_activity_at = None
        self._last_pulse_at = 0.0
        self._lock = threading.Lock()
        self._watchdog_thread = None
        self._stop_watchdog = threading.Event()

        if _HARDWARE_AVAILABLE:
            GPIO.setmode(GPIO.BOARD)
            pull = GPIO.PUD_UP if self.active_low else GPIO.PUD_DOWN
            GPIO.setup(self.coin_pin, GPIO.IN, pull_up_down=pull)
            GPIO.setup(self.relay_pin, GPIO.OUT, initial=GPIO.LOW)
            edge = GPIO.FALLING if self.active_low else GPIO.RISING
            GPIO.add_event_detect(
                self.coin_pin, edge, callback=self._raw_pulse, bouncetime=0
            )
            # bouncetime=0: we do our own debounce below via pulse-width
            # filtering rather than relying on OPi.GPIO's built-in
            # debounce, since the MPD calls for a minimum pulse-width
            # filter specifically (not just a fixed ignore-period).

    # --- public API ---------------------------------------------------

    def arm(self, session_token=None):
        """
        `session_token` identifies which session's "Insert Coin" tap
        triggered this arm -- pulses received while armed are attributed
        to whoever is CURRENTLY recorded here, read fresh at pulse time
        (not captured into a closure).

        Returns True if this call successfully armed (or refreshed) the
        acceptor. Returns False if a DIFFERENT session_token is already
        armed -- the caller must not treat this as success. The
        already-armed session is completely unaffected by a rejected
        call: no state changes, no window reset, on the rejected side.

        A session re-arming with its OWN, already-armed session_token is
        allowed and treated as a deliberate window refresh (not a
        conflict) -- this covers a device re-tapping "Insert Coin" while
        its own popup is still open.
        """
        with self._lock:
            if self._armed and self._armed_session_token != session_token:
                return False
            self._armed = True
            self._armed_session_token = session_token
            self._arm_started_at = time.time()
            self._last_activity_at = self._arm_started_at
        if _HARDWARE_AVAILABLE:
            GPIO.output(self.relay_pin, GPIO.HIGH)
        else:
            logger.info("[SIMULATED] Relay armed for session_token=%s.", session_token)
        self._start_watchdog()
        return True

    def disarm(self):
        with self._lock:
            self._armed = False
            self._armed_session_token = None
        if _HARDWARE_AVAILABLE:
            GPIO.output(self.relay_pin, GPIO.LOW)
        else:
            logger.info("[SIMULATED] Relay disarmed.")
        self._stop_watchdog_thread()

    def is_armed(self):
        with self._lock:
            return self._armed

    def get_armed_session_token(self):
        with self._lock:
            return self._armed_session_token

    def get_armed_remaining_seconds(self):
        with self._lock:
            if not self._armed:
                return 0
            idle_for = time.time() - self._last_activity_at
        remaining = config.ARM_ACCEPT_WINDOW_SECONDS - idle_for
        return max(0, round(remaining))

    def simulate_pulse(self):
        """Test/dev-only hook -- lets the portal or a test script simulate
        a coin insertion without real hardware. Never called from
        production GPIO code paths."""
        self._raw_pulse(self.coin_pin)

    # --- internals ------------------------------------------------------

    def _raw_pulse(self, channel):
        now = time.time()

        # Debounce: minimum pulse-width filter (rule #3) -- reject pulses
        # arriving faster than DEBOUNCE_SECONDS apart as electrical noise,
        # not real coins.
        if now - self._last_pulse_at < config.DEBOUNCE_SECONDS:
            return
        self._last_pulse_at = now

        with self._lock:
            if not self._armed:
                return
            elapsed_since_arm = now - self._arm_started_at
            if elapsed_since_arm < config.ARM_IGNORE_WINDOW_SECONDS:
                # Power-up phantom-pulse suppression window (rule #4).
                logger.debug("Pulse ignored -- inside 1.5s post-arm suppression window.")
                return
            self._last_activity_at = now

        self.on_coin()

    def _start_watchdog(self):
        self._stop_watchdog.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()

    def _stop_watchdog_thread(self):
        self._stop_watchdog.set()

    def _watchdog_loop(self):
        """Auto-disarm after ARM_ACCEPT_WINDOW_SECONDS of no activity,
        per rule #4 -- the window resets on each valid coin (handled by
        _raw_pulse updating _last_activity_at)."""
        while not self._stop_watchdog.is_set():
            time.sleep(0.5)
            with self._lock:
                if not self._armed:
                    return
                idle_for = time.time() - self._last_activity_at
                if idle_for >= config.ARM_ACCEPT_WINDOW_SECONDS:
                    should_disarm = True
                else:
                    should_disarm = False
            if should_disarm:
                logger.info("Coin acceptor auto-disarmed -- accept window expired.")
                self.disarm()
                return

    def cleanup(self):
        self._stop_watchdog_thread()
        if _HARDWARE_AVAILABLE:
            GPIO.cleanup()
