#!/bin/bash
set -e

if command -v systemctl &>/dev/null; then
    systemctl daemon-reload
fi

# On full removal, clean up /opt/zpbs-backup
# deb: $1 = "purge"; rpm: $1 = 0 (final removal)
if [ "$1" = "purge" ] || [ "$1" = "0" ]; then
    rm -rf /opt/zpbs-backup
fi
