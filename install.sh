#!/usr/bin/env bash
#
# install.sh — Barathrum Box Agent manual install (Option 1 deployment path)
#
# Target: a freshly-booted STOCK Armbian image (Ubuntu 26.04 or Debian 13,
# from armbian.com/boards/orangepione) for the Orange Pi One, installed
# manually over SSH after Etcher-flashing the SD card.
#
# What this script does:
#   1. Full apt update/upgrade
#   2. Install system dependencies (python3, venv, pip, git, iptables,
#      hostapd, dnsmasq — only if missing)
#   3. Clone or update this repo into /opt/barathrum-box-agent
#   4. Create a venv there and pip install requirements.txt
#   5. Create /var/lib/barathrum (BARATHRUM_DATA_DIR) with correct ownership
#   6. Install + enable (but NOT start) the systemd service
#   7. Print the real-hardware placeholder checklist from config.py
#
# What this script deliberately does NOT do (see dev prompt "Explicit
# non-goals" for the full reasoning):
#   - Does not `systemctl start` the agent. Starting it before the
#     hardware placeholders below are confirmed against the real board
#     risks the agent actively driving GPIO/network with wrong values.
#   - Does not modify config.py or systemd/barathrum-agent.service.
#   - Does not build the Option 2 custom-image path.
#   - Does not auto-detect or hardcode a WiFi adapter interface name.
#
# Safe to re-run: every step below is idempotent — re-running this script
# on a box that's already been set up will not duplicate the systemd unit,
# will not clobber uncommitted local changes in the repo, and will not
# error out because a step was already done.
#
# Must be run as root (needed for apt, /opt, /var/lib, systemd).

set -euo pipefail

# --- Config ---------------------------------------------------------------

REPO_URL="https://github.com/benzgarcel122-crypto/barathrum-box-agent.git"
INSTALL_DIR="/opt/barathrum-box-agent"
DATA_DIR="/var/lib/barathrum"
SERVICE_NAME="barathrum-agent.service"
SERVICE_SRC="${INSTALL_DIR}/systemd/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

# --- Helpers ----------------------------------------------------------------

log() {
    echo "[install.sh] $*"
}

fail() {
    echo "[install.sh] ERROR: $*" >&2
    exit 1
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        fail "must be run as root (e.g. sudo ./install.sh)."
    fi
}

binary_exists() {
    command -v "$1" >/dev/null 2>&1
}

# --- Steps --------------------------------------------------------------

step_apt_update() {
    log "Step 1/7: apt update && apt full-upgrade -y"
    apt-get update -y || fail "apt-get update failed."

    # Capture whether the kernel package gets upgraded, so we can recommend
    # a reboot afterward without forcing one.
    local kernel_will_upgrade
    kernel_will_upgrade=$(apt-get -s full-upgrade 2>/dev/null | grep -c '^Inst linux-image' || true)

    apt-get full-upgrade -y || fail "apt-get full-upgrade failed."

    if [[ "${kernel_will_upgrade}" -gt 0 ]]; then
        REBOOT_RECOMMENDED=1
    else
        REBOOT_RECOMMENDED=0
    fi
}

step_install_dependencies() {
    log "Step 2/7: installing system dependencies (only what's missing)"

    local to_install=()

    for pkg_bin in "python3:python3" "pip3:python3-pip" "git:git"; do
        local bin="${pkg_bin%%:*}"
        local pkg="${pkg_bin##*:}"
        if ! binary_exists "${bin}"; then
            to_install+=("${pkg}")
        fi
    done

    # python3-venv doesn't expose a distinctly-named binary; check the module
    # itself rather than a binary that might not exist.
    if ! python3 -c "import venv" >/dev/null 2>&1; then
        to_install+=("python3-venv")
    fi

    if ! binary_exists iptables; then
        to_install+=("iptables")
    fi

    if ! binary_exists hostapd; then
        to_install+=("hostapd")
    fi

    if ! binary_exists dnsmasq; then
        to_install+=("dnsmasq")
    fi

    if [[ "${#to_install[@]}" -gt 0 ]]; then
        log "Installing: ${to_install[*]}"
        apt-get install -y "${to_install[@]}" || fail "apt-get install failed for: ${to_install[*]}"
    else
        log "All system dependencies already present — nothing to install."
    fi
}

step_clone_or_update_repo() {
    log "Step 3/7: cloning/updating ${REPO_URL} into ${INSTALL_DIR}"

    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        log "${INSTALL_DIR} already a git checkout — fetching and resetting to origin."
        git -C "${INSTALL_DIR}" fetch origin || fail "git fetch failed in ${INSTALL_DIR}."
        # Determine origin's default branch rather than assuming 'main'.
        local default_branch
        default_branch=$(git -C "${INSTALL_DIR}" remote show origin 2>/dev/null \
            | sed -n '/HEAD branch/s/.*: //p')
        default_branch="${default_branch:-main}"
        git -C "${INSTALL_DIR}" checkout "${default_branch}" || fail "git checkout ${default_branch} failed."
        git -C "${INSTALL_DIR}" reset --hard "origin/${default_branch}" \
            || fail "git reset --hard failed in ${INSTALL_DIR}."
    elif [[ -d "${INSTALL_DIR}" ]]; then
        fail "${INSTALL_DIR} exists but is not a git checkout. Refusing to overwrite" \
             "a directory this script doesn't recognize — inspect it manually first."
    else
        git clone "${REPO_URL}" "${INSTALL_DIR}" || fail "git clone failed."
    fi
}

step_setup_venv() {
    log "Step 4/7: creating venv and installing requirements.txt"

    if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/venv" || fail "venv creation failed."
    else
        log "venv already exists at ${INSTALL_DIR}/venv — reusing it."
    fi

    "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip \
        || fail "pip upgrade inside venv failed."
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" \
        || fail "pip install -r requirements.txt failed."
}

step_create_data_dir() {
    log "Step 5/7: creating ${DATA_DIR}"

    mkdir -p "${DATA_DIR}"
    # Service runs as root (systemd unit's own User=root, required for
    # iptables/hostapd/dnsmasq/GPIO/port 80) — data dir owned by root:root
    # accordingly. Not touching group/other permissions beyond a sane default.
    chown root:root "${DATA_DIR}"
    chmod 750 "${DATA_DIR}"
}

step_install_systemd_service() {
    log "Step 6/7: installing systemd service (enable only, NOT starting it)"

    if [[ ! -f "${SERVICE_SRC}" ]]; then
        fail "${SERVICE_SRC} not found — repo checkout looks incomplete."
    fi

    cp "${SERVICE_SRC}" "${SERVICE_DST}" || fail "copying ${SERVICE_SRC} to ${SERVICE_DST} failed."
    systemctl daemon-reload || fail "systemctl daemon-reload failed."
    systemctl enable "${SERVICE_NAME}" || fail "systemctl enable ${SERVICE_NAME} failed."

    log "Service installed and enabled (will start on future boots once you"
    log "start it manually the first time — see the checklist below). This"
    log "script deliberately does NOT run 'systemctl start ${SERVICE_NAME}'."
}

step_print_placeholder_checklist() {
    log "Step 7/7: real-hardware placeholder checklist"
    cat <<'EOF'

================================================================================
 BEFORE YOU START THE SERVICE FOR THE FIRST TIME
================================================================================
The following values in config.py are PLACEHOLDERS only — confirm/edit them
against the real Orange Pi One and its wired hardware before running:

    systemctl start barathrum-agent.service

  - config.DEFAULT_COIN_PIN / config.DEFAULT_RELAY_PIN
      BOARD numbering. Community sources report the Orange Pi One's header
      is 180°-flipped relative to Orange Pi PC / Raspberry Pi — do NOT trust
      these numbers without checking the real board's pinout first.

  - config.LAN_IFACE (currently defaults to "wlan0")
      The Orange Pi One has no onboard WiFi. Confirm the real interface name
      of the USB WiFi adapter via `ip link` once it's plugged in — this may
      not be wlan0.

  - config.DEBOUNCE_SECONDS (currently 0.05s)
  - config.DEFAULT_PULSE_ACTIVE_LOW (currently True)
      Neither has been measured against the real coin acceptor — tune
      against its datasheet or by observation once wired up.

This script installed the code and enabled the systemd service, but did NOT
start it and did NOT touch any of the values above. Confirm them in
config.py first, then run:

    systemctl start barathrum-agent.service
    systemctl status barathrum-agent.service
================================================================================

EOF

    if [[ "${REBOOT_RECOMMENDED:-0}" -eq 1 ]]; then
        log "NOTE: a kernel package was upgraded during Step 1. A reboot is"
        log "recommended before starting the service, but this script will"
        log "not reboot automatically — reboot manually when convenient."
    fi
}

# --- Main -----------------------------------------------------------------

main() {
    require_root
    step_apt_update
    step_install_dependencies
    step_clone_or_update_repo
    step_setup_venv
    step_create_data_dir
    step_install_systemd_service
    step_print_placeholder_checklist
    log "Install complete."
}

main "$@"
