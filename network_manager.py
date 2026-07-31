"""
Network control layer: hostapd (LAN/AP), dnsmasq (DHCP/DNS), iptables
(MAC-based access control + NAT). STEP 1 rules #2 and #8.

Fail-closed by construction (rule #8): the base iptables policy is DROP
on the FORWARD chain, with explicit ACCEPT rules only for granted MACs.
Any failure to write/apply a rule must surface as "no access granted",
never a silent pass-through.

All actual system calls go through `_run()`, which is deliberately the
single choke point for subprocess execution -- makes it possible to run
this whole module in a DRY_RUN mode (see below) for development/testing
away from the real box, and keeps every real system-mutating call
auditable in one place.

Root privileges are required for all of this in production (iptables,
hostapd/dnsmasq service control) -- the systemd unit (see systemd/) runs
this agent as root accordingly.
"""

import logging
import os
import subprocess

import config

logger = logging.getLogger("barathrum.network")

# When true, no real subprocess calls are made -- every command is logged
# instead. Set BARATHRUM_DRY_RUN=1 in the environment for local/dev
# testing away from the real box. Production systemd unit does not set
# this, so it defaults to real execution there.
DRY_RUN = os.environ.get("BARATHRUM_DRY_RUN", "0") == "1"


def _run(cmd, check=True):
    logger.debug("RUN: %s", " ".join(cmd))
    if DRY_RUN:
        logger.info("[DRY_RUN] would run: %s", " ".join(cmd))
        return
    try:
        subprocess.run(cmd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Command failed (%s): stdout=%r stderr=%r",
            " ".join(cmd), exc.stdout, exc.stderr,
        )
        raise


# --- iptables: fail-closed base policy + NAT ----------------------------

def apply_base_firewall_policy():
    """
    Must be called once on every boot, BEFORE any MAC grants are applied.
    Sets up: default-DROP forwarding, NAT masquerade for granted traffic
    out the WAN interface, and allows the box's own admin/portal/DHCP/DNS
    traffic on the LAN side regardless of grant status (so the captive
    portal and admin panel are always reachable even with zero active
    sessions).
    """
    commands = [
        ["iptables", "-P", "FORWARD", "DROP"],
        ["iptables", "-F", "FORWARD"],
        ["iptables", "-t", "nat", "-F", "POSTROUTING"],
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", config.WAN_IFACE, "-j", "MASQUERADE"],
        # Always allow LAN devices to reach the box itself (portal on
        # GATEWAY_IP, port 80/443, DHCP, DNS) regardless of grant status --
        # this is how an ungranted customer even sees the captive portal.
        ["iptables", "-A", "INPUT", "-i", config.LAN_IFACE, "-p", "tcp",
         "--dport", "80", "-j", "ACCEPT"],
        ["iptables", "-A", "INPUT", "-i", config.LAN_IFACE, "-p", "udp",
         "--dport", "67:68", "-j", "ACCEPT"],  # DHCP
        ["iptables", "-A", "INPUT", "-i", config.LAN_IFACE, "-p", "udp",
         "--dport", "53", "-j", "ACCEPT"],  # DNS
    ]
    for cmd in commands:
        _run(cmd, check=False)  # check=False: some of these may legitimately
                                 # no-op or already exist on a warm restart
    logger.info("Base firewall policy applied (default DROP, NAT configured).")


def grant_mac(mac_address):
    """Explicit ACCEPT for this MAC's forwarded traffic. Idempotent --
    safe to call even if a rule for this MAC already exists (revoke first
    to avoid duplicate rules piling up over many grant/revoke cycles)."""
    revoke_mac(mac_address, quiet=True)
    _run([
        "iptables", "-I", "FORWARD", "1",
        "-m", "mac", "--mac-source", mac_address,
        "-j", "ACCEPT",
    ])
    logger.info("Access GRANTED for MAC %s", mac_address)


def revoke_mac(mac_address, quiet=False):
    """Removes the ACCEPT rule for this MAC, if present. Safe to call on
    a MAC with no existing rule (e.g. at session-zero, or as a defensive
    pre-step inside grant_mac)."""
    _run([
        "iptables", "-D", "FORWARD",
        "-m", "mac", "--mac-source", mac_address,
        "-j", "ACCEPT",
    ], check=False)
    if not quiet:
        logger.info("Access REVOKED for MAC %s", mac_address)


# --- hostapd / dnsmasq config generation --------------------------------

_HOSTAPD_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "config", "hostapd.conf.template"
)
_DNSMASQ_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "config", "dnsmasq.conf.template"
)
_HOSTAPD_RENDERED_PATH = "/etc/hostapd/hostapd.conf"
_DNSMASQ_RENDERED_PATH = "/etc/dnsmasq.conf"


def render_hostapd_config(ssid, passphrase):
    """
    passphrase="" (the Setup Wizard's temporary open network,
    broadcast_setup_network() below) omits the wpa* lines entirely --
    hostapd fails to start if wpa=2 is present with an empty
    wpa_passphrase, it does not just "run open" as an earlier version of
    this function assumed. A non-empty passphrase renders the normal
    WPA2-PSK block.
    """
    with open(_HOSTAPD_TEMPLATE_PATH) as f:
        template = f.read()

    if passphrase:
        wpa_block = (
            "wpa=2\n"
            f"wpa_passphrase={passphrase}\n"
            "wpa_key_mgmt=WPA-PSK\n"
            "wpa_pairwise=CCMP\n"
            "rsn_pairwise=CCMP"
        )
    else:
        wpa_block = ""

    rendered = (
        template
        .replace("{{INTERFACE}}", config.LAN_IFACE)
        .replace("{{SSID}}", ssid)
        .replace("{{WPA_BLOCK}}", wpa_block)
    )
    if DRY_RUN:
        logger.info("[DRY_RUN] would write hostapd config:\n%s", rendered)
        return
    with open(_HOSTAPD_RENDERED_PATH, "w") as f:
        f.write(rendered)


def render_dnsmasq_config():
    with open(_DNSMASQ_TEMPLATE_PATH) as f:
        template = f.read()
    rendered = (
        template
        .replace("{{INTERFACE}}", config.LAN_IFACE)
        .replace("{{GATEWAY_IP}}", config.GATEWAY_IP)
        .replace("{{DHCP_RANGE_START}}", config.DHCP_RANGE_START)
        .replace("{{DHCP_RANGE_END}}", config.DHCP_RANGE_END)
        .replace("{{DHCP_LEASE_TIME}}", config.DHCP_LEASE_TIME)
    )
    if DRY_RUN:
        logger.info("[DRY_RUN] would write dnsmasq config:\n%s", rendered)
        return
    with open(_DNSMASQ_RENDERED_PATH, "w") as f:
        f.write(rendered)


def restart_network_services():
    _run(["systemctl", "restart", "hostapd"])
    _run(["systemctl", "restart", "dnsmasq"])
    logger.info("hostapd + dnsmasq restarted.")


def set_customer_wifi(ssid, passphrase):
    """Called from the Setup Wizard and from the admin panel's Settings
    tab (SSID/password change)."""
    render_hostapd_config(ssid, passphrase)
    restart_network_services()


def broadcast_setup_network(box_mac_suffix):
    """First-boot, no-license-yet state (rule #11): temporary unbranded
    setup network so the operator can pair the box."""
    ssid = f"{config.SETUP_SSID_PREFIX}{box_mac_suffix}"
    render_hostapd_config(ssid, passphrase="")  # open network for setup
    render_dnsmasq_config()
    restart_network_services()
    logger.info("Broadcasting temporary setup network: %s", ssid)
