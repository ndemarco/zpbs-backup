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
rm -f "${VENV_DIR}/bin/python3"
cp "${PYTHON}" "${VENV_DIR}/bin/python3"
ln -sf python3 "${VENV_DIR}/bin/python"

# Update pyvenv.cfg to reference the target system's python location
# (it was set to the CI build host's path during package build)
sed -i "s|^home = .*|home = ${PYTHON_DIR}|" "${VENV_DIR}/pyvenv.cfg"

# Handle Python version mismatch between build and target system.
# The venv was built with CI's Python (e.g. 3.11) but target may have 3.12+.
# Python looks for lib/pythonX.Y/ matching its own version, so create a
# symlink from the target version to the build version if they differ.
SYS_PY_VER=$("${VENV_DIR}/bin/python3" -c "import sys; print(f'python{sys.version_info.major}.{sys.version_info.minor}')")
BUILD_PY_DIR=$(ls -d "${VENV_DIR}/lib/python"* 2>/dev/null | head -1)
if [ -n "${BUILD_PY_DIR}" ]; then
    BUILD_PY_VER=$(basename "${BUILD_PY_DIR}")
    if [ "${SYS_PY_VER}" != "${BUILD_PY_VER}" ]; then
        ln -sfn "${BUILD_PY_VER}" "${VENV_DIR}/lib/${SYS_PY_VER}"
    fi
fi

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
