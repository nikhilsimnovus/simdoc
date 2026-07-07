#!/bin/bash
# SimDoc one-shot installer (oneclick-style).
#
# Runs on Ubuntu/Debian or RHEL-family hosts. Creates the simdoc user, lays
# down files into /opt/simdoc, builds a Python venv with Flask + requests +
# Playwright (downloads headless Chromium), installs + starts the systemd
# unit on port 7000. Idempotent — safe to re-run; the Update button in the
# UI re-runs this same script from the latest GitHub tarball.
#
# Usage:
#   sudo bash scripts/install.sh
#
# Override the listen port (default 7000) by exporting SIMDOC_PORT first.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: install.sh must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "${SCRIPT_DIR}")"
[[ -f "${SRC_ROOT}/exporter.py" ]] || {
    echo "ERROR: expected ${SRC_ROOT}/exporter.py; running from wrong dir?" >&2
    exit 1
}

# ---- Knobs -----------------------------------------------------------------
SERVICE_USER="${SIMDOC_USER:-simdoc}"
SERVICE_GROUP="${SIMDOC_GROUP:-simdoc}"
SIMDOC_HOME="${SIMDOC_HOME:-/var/lib/simdoc}"
INSTALL_DIR="${SIMDOC_INSTALL_DIR:-/opt/simdoc}"
PDF_DIR="${SIMDOC_OUTPUT:-/var/lib/simdoc/pdfs}"
CONFIG_FILE="${SIMDOC_CONFIG:-/var/lib/simdoc/config.json}"
LISTEN_PORT="${SIMDOC_PORT:-7000}"
SYSTEMD_UNIT="/etc/systemd/system/simdoc.service"
PW_BROWSERS="${SIMDOC_HOME}/.cache/ms-playwright"
UPDATE_TARBALL_URL_DEFAULT="https://github.com/nikhilsimnovus/simdoc/archive/refs/heads/main.tar.gz"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 1) OS prereqs ----------------------------------------------------------
if   command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
elif command -v yum     >/dev/null 2>&1; then PKG=yum
else fail "no supported package manager (apt-get / dnf / yum)"
fi
log "Using ${PKG}"
case "${PKG}" in
  apt)
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        bash python3 python3-venv python3-pip curl tar ca-certificates
    ;;
  dnf|yum)
    ${PKG} install -y -q bash python3 python3-pip curl tar ca-certificates
    ;;
esac

# ---- 2) Service user + dirs -------------------------------------------------
if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    log "User ${SERVICE_USER} already exists"
else
    log "Creating user ${SERVICE_USER} (home=${SIMDOC_HOME})"
    useradd --system --create-home --home "${SIMDOC_HOME}" --shell /bin/bash "${SERVICE_USER}"
fi
install -d -m 0755 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
    "${INSTALL_DIR}" "${PDF_DIR}" "$(dirname "${CONFIG_FILE}")"

# ---- 3) Copy files ----------------------------------------------------------
log "Installing SimDoc to ${INSTALL_DIR}"
install -m 0644 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
    "${SRC_ROOT}/exporter.py" \
    "${SRC_ROOT}/ui/app.py" \
    "${SRC_ROOT}/ui/favicon.png" \
    "${SRC_ROOT}/ui/logo_light.svg" \
    "${SRC_ROOT}/ui/logo_dark.svg" \
    "${INSTALL_DIR}/"

# ---- 4) Python venv ----------------------------------------------------------
if [[ -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    log "Venv exists at ${INSTALL_DIR}/venv — upgrading deps"
else
    log "Creating Python venv at ${INSTALL_DIR}/venv"
    sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/venv"
fi

pip_install() {
    local logf; logf="$(mktemp)"
    if sudo -u "${SERVICE_USER}" -E "${INSTALL_DIR}/venv/bin/pip" install --quiet "$@" >"$logf" 2>&1; then
        rm -f "$logf"; return 0
    fi
    if grep -qE 'CERTIFICATE_VERIFY_FAILED|SSLError|self-signed certificate' "$logf"; then
        warn "pip TLS verification failed (SSL-inspecting proxy?) — retrying with --trusted-host"
        if sudo -u "${SERVICE_USER}" -E "${INSTALL_DIR}/venv/bin/pip" install --quiet \
                --trusted-host pypi.org --trusted-host files.pythonhosted.org \
                --trusted-host pypi.python.org "$@" >"$logf" 2>&1; then
            rm -f "$logf"; return 0
        fi
    fi
    echo "----- pip output (last 30 lines) -----" >&2
    tail -30 "$logf" >&2; rm -f "$logf"
    fail "pip install failed for: $*"
}
log "Installing Flask + requests + Playwright into venv"
pip_install --upgrade pip
pip_install --upgrade flask requests playwright

# ---- 4b) Playwright Chromium (PDF renderer) ----------------------------------
has_chromium() { compgen -G "$1/chromium-*" >/dev/null 2>&1; }
if has_chromium "${PW_BROWSERS}"; then
    log "Playwright Chromium already present in ${PW_BROWSERS}"
else
    log "Downloading Playwright Chromium (~150 MB, one-shot)"
    sudo -u "${SERVICE_USER}" env PLAYWRIGHT_BROWSERS_PATH="${PW_BROWSERS}" \
        "${INSTALL_DIR}/venv/bin/playwright" install chromium || \
        fail "playwright install chromium failed (check outbound HTTPS to cdn.playwright.dev)"
fi
if [[ "${PKG}" == "apt" ]]; then
    log "Installing headless-Chromium OS libs via playwright install-deps"
    "${INSTALL_DIR}/venv/bin/playwright" install-deps chromium || \
        warn "playwright install-deps failed; some libs may be missing"
fi

# ---- 5) systemd unit ----------------------------------------------------------
log "Installing systemd unit -> ${SYSTEMD_UNIT}"
cat > "${SYSTEMD_UNIT}" <<UNIT
[Unit]
Description=SimDoc handbook-to-PDF UI (Flask)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${INSTALL_DIR}
Environment=HOME=${SIMDOC_HOME}
Environment=USER=${SERVICE_USER}
Environment=SIMDOC_PORT=${LISTEN_PORT}
Environment=SIMDOC_CONFIG=${CONFIG_FILE}
Environment=SIMDOC_OUTPUT=${PDF_DIR}
Environment=PLAYWRIGHT_BROWSERS_PATH=${PW_BROWSERS}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# ---- 5b) Self-update plumbing --------------------------------------------------
# The UI's Update button (POST /api/update) re-runs this installer from the
# latest GitHub tarball. The service user has no sudo by default, so plant:
#   * /usr/local/sbin/simdoc-update — downloads the tarball, re-runs install.sh
#   * /etc/sudoers.d/simdoc        — NOPASSWD entry for *only* that script
UPDATER_PATH="/usr/local/sbin/simdoc-update"
log "Installing self-update helper -> ${UPDATER_PATH}"
cat > "${UPDATER_PATH}" <<UPDATER
#!/bin/bash
# Auto-generated by SimDoc install.sh. Triggered by the Update button in
# the Flask UI (POST /api/update). Downloads the latest tarball from the
# simdoc GitHub repo and re-runs scripts/install.sh from it.
set -euo pipefail
TARBALL_URL="\${SIMDOC_UPDATE_TARBALL:-${UPDATE_TARBALL_URL_DEFAULT}}"
TD=\$(mktemp -d -p /tmp simdoc-update-XXXXXX)
trap 'rm -rf "\$TD"' EXIT
echo "[simdoc-update] downloading \$TARBALL_URL"
if ! curl -fsSL "\$TARBALL_URL" -o "\$TD/main.tar.gz" 2>"\$TD/curl.err"; then
    if grep -qiE 'self.signed|certificate|SSL' "\$TD/curl.err"; then
        echo "[simdoc-update] SSL verification failed (corporate proxy?) — retrying with -k" >&2
        curl -fkSL "\$TARBALL_URL" -o "\$TD/main.tar.gz"
    else
        cat "\$TD/curl.err" >&2
        exit 1
    fi
fi
tar xzf "\$TD/main.tar.gz" -C "\$TD" --strip-components=1
exec bash "\$TD/scripts/install.sh"
UPDATER
chmod 0755 "${UPDATER_PATH}"

SUDOERS_FILE="/etc/sudoers.d/simdoc"
log "Granting ${SERVICE_USER} passwordless sudo for ${UPDATER_PATH} only"
cat > "${SUDOERS_FILE}" <<SUDO
# Auto-generated by SimDoc install.sh. Lets the simdoc service trigger a
# self-update via the Update button WITHOUT general sudo.
${SERVICE_USER} ALL=(root) NOPASSWD: ${UPDATER_PATH}
SUDO
chmod 0440 "${SUDOERS_FILE}"
if ! visudo -c -f "${SUDOERS_FILE}" >/dev/null 2>&1; then
    warn "sudoers entry has a syntax issue — removing to keep sudo working"
    rm -f "${SUDOERS_FILE}"
fi

systemctl daemon-reload
log "Enabling + starting simdoc"
systemctl enable --now simdoc.service
systemctl restart simdoc.service
sleep 2
if systemctl is-active --quiet simdoc.service; then
    log "simdoc is ACTIVE"
else
    warn "simdoc failed to start — see: journalctl -u simdoc -n 50"
    systemctl status simdoc --no-pager || true
    exit 1
fi

HOSTNAME_BEST=$(hostname -I 2>/dev/null | awk '{print $1}')
[[ -z "${HOSTNAME_BEST}" ]] && HOSTNAME_BEST="<host-ip>"
cat <<DONE

============================================================
  SimDoc is installed and running.
============================================================

  URL:      http://${HOSTNAME_BEST}:${LISTEN_PORT}/
  Service:  systemctl status simdoc
  Logs:     journalctl -u simdoc -f
  Config:   ${CONFIG_FILE}
  PDFs:     ${PDF_DIR}

Next steps:
  1. Open the URL, expand Settings, enter the Atlassian email + API token.
  2. Type the release version, click Generate PDF.

Uninstall:
  sudo systemctl disable --now simdoc
  sudo rm -f ${SYSTEMD_UNIT} /etc/sudoers.d/simdoc /usr/local/sbin/simdoc-update
  sudo rm -rf ${INSTALL_DIR} ${SIMDOC_HOME}
  sudo userdel ${SERVICE_USER}
  sudo systemctl daemon-reload
DONE
