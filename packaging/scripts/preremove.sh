#!/bin/bash
set -e

if command -v systemctl &>/dev/null; then
    # Stop and disable on full removal (not upgrade)
    # deb: $1 = "remove"; rpm: $1 = 0 (final removal)
    if [ "$1" = "remove" ] || [ "$1" = "0" ]; then
        systemctl stop zpbs-backup.timer 2>/dev/null || true
        systemctl stop zpbs-backup.service 2>/dev/null || true
        systemctl disable zpbs-backup.timer 2>/dev/null || true
    fi
fi
