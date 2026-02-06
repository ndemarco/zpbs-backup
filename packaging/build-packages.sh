#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

# Extract version from __init__.py (single source of truth)
VERSION=$(python3 -c "
import re
with open('src/zpbs_backup/__init__.py') as f:
    match = re.search(r'__version__\s*=\s*\"(.*?)\"', f.read())
    print(match.group(1))
")
export VERSION

echo "==> Building zpbs-backup ${VERSION}"

# 1. Build the wheel if not already present
WHEEL="dist/zpbs_backup-${VERSION}-py3-none-any.whl"
if [ ! -f "${WHEEL}" ]; then
    echo "==> Building wheel..."
    python3 -m build --wheel
fi

# 2. Create the embedded virtualenv
BUILD_DIR="./build"
rm -rf "${BUILD_DIR}/venv"
echo "==> Creating virtualenv..."
python3 -m venv "${BUILD_DIR}/venv" --without-pip

# Install pip into the venv, then install the wheel
"${BUILD_DIR}/venv/bin/python" -m ensurepip --upgrade --default-pip 2>/dev/null || \
    "${BUILD_DIR}/venv/bin/python" -m ensurepip --upgrade
"${BUILD_DIR}/venv/bin/pip" install --no-cache-dir "${WHEEL}"

# 3. Strip unnecessary files to reduce package size
echo "==> Stripping venv..."
"${BUILD_DIR}/venv/bin/pip" uninstall -y pip setuptools 2>/dev/null || true
find "${BUILD_DIR}/venv" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "${BUILD_DIR}/venv" -type d -name "*.dist-info" -exec sh -c 'find "$1" -name "RECORD" -delete' _ {} \; 2>/dev/null || true
rm -rf "${BUILD_DIR}/venv/lib/python*/ensurepip" 2>/dev/null || true

# 4. Rewrite shebangs to use the installed path
echo "==> Fixing shebangs..."
find "${BUILD_DIR}/venv/bin" -type f | while read -r file; do
    if head -1 "$file" 2>/dev/null | grep -q "^#!.*python"; then
        sed -i "1s|^#!.*python.*|#!/opt/zpbs-backup/venv/bin/python3|" "$file"
    fi
done

# 5. Remove the python symlinks (postinstall.sh will recreate them pointing to system python)
rm -f "${BUILD_DIR}/venv/bin/python" "${BUILD_DIR}/venv/bin/python3" "${BUILD_DIR}/venv/bin/python3.*"

# 6. Create the wrapper script
cat > "${BUILD_DIR}/zpbs-backup-wrapper" << 'WRAPPER'
#!/bin/bash
exec /opt/zpbs-backup/venv/bin/zpbs-backup "$@"
WRAPPER
chmod 755 "${BUILD_DIR}/zpbs-backup-wrapper"

# 7. Build packages with nfpm
echo "==> Building packages..."
ARCHES="${ARCHES:-amd64 arm64}"
for ARCH in ${ARCHES}; do
    export ARCH

    # Map arch names for RPM (nfpm handles this, but be explicit for filenames)
    RPM_ARCH="${ARCH}"
    [ "${ARCH}" = "amd64" ] && RPM_ARCH="x86_64"
    [ "${ARCH}" = "arm64" ] && RPM_ARCH="aarch64"

    echo "  -> deb ${ARCH}"
    nfpm package \
        --config packaging/nfpm.yaml \
        --packager deb \
        --target "dist/zpbs-backup_${VERSION}_${ARCH}.deb"

    echo "  -> rpm ${RPM_ARCH}"
    nfpm package \
        --config packaging/nfpm.yaml \
        --packager rpm \
        --target "dist/zpbs-backup-${VERSION}.${RPM_ARCH}.rpm"
done

echo ""
echo "==> Packages built:"
ls -lh dist/zpbs-backup*"${VERSION}"* 2>/dev/null || echo "  (none found)"
