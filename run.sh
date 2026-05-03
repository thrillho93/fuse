#!/usr/bin/env bash
# Run Fuse directly from source using system Python and GTK.
# Requires: python-gobject, gtk4, libadwaita, ffmpeg
set -euo pipefail
cd "$(dirname "$0")"
exec python3 fuse.py "$@"
