#!/bin/bash
set -e

VENV_DIR=/opt/zpbs-backup/venv

# Fix up python symlink to point to system python >= 3.11
# Try python3 first (Debian 12, Ubuntu 24.04), then python3.11 (RHEL 9)
if python3 -c "import sys; assert sys.version_info >= (3, 11)" 2>/dev/null; then
    PYTHON=$(command -v python3)
elif python3.11 --version &>/dev/null; then
    PYTHON=$(command -v python3.11)
else
    echo "ERROR: zpbs-backup requires Python >= 3.11" >&2
    exit 1
fi

# Resolve to real path (in case python3 is itself a symlink)
PYTHON=$(readlink -f "${PYTHON}")
PYTHON_DIR=$(dirname "${PYTHON}")

# Copy system python into venv (symlinks break pyvenv.cfg discovery,
# because Python resolves symlinks before searching for pyvenv.cfg)
cp "${PYTHON}" "${VENV_DIR}/bin/python3"
ln -sf python3 "${VENV_DIR}/bin/python"

# Update pyvenv.cfg to reference the target system's python location
# (it was set to the CI build host's path during package build)
sed -i "s|^home = .*|home = ${PYTHON_DIR}|" "${VENV_DIR}/pyvenv.cfg"

# Systemd integration
if command -v systemctl &>/dev/null; then
    systemctl daemon-reload

    # Enable timer on fresh install (not on upgrade)
    # deb: $1 = "configure"; rpm: $1 = 1 (install count)
    if [ "$1" = "configure" ] || [ "$1" = "1" ]; then
        systemctl enable zpbs-backup.timer || true
        echo ""
        echo "zpbs-backup installed successfully."
        echo "  Timer enabled (daily at 2:00 AM)."
        echo "  Configure PBS connection: /etc/zpbs-backup/pbs.conf"
        echo "  Then start the timer:     systemctl start zpbs-backup.timer"
        echo ""
    fi
fi
