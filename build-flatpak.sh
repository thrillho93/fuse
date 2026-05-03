#!/usr/bin/env bash
# Build and install a local Flatpak of Fuse.
#
# First run downloads org.gnome.Platform/Sdk 47 and
# org.freedesktop.Platform.ffmpeg-full 24.08 from Flathub (~1 GB total).
# Subsequent builds reuse the cached runtimes and are much faster.

set -euo pipefail
cd "$(dirname "$0")"

BUILD_DIR="${FUSE_FLATPAK_BUILD_DIR:-/tmp/fuse-flatpak-build}"
STATE_DIR="${FUSE_FLATPAK_STATE_DIR:-/tmp/fuse-flatpak-state}"

echo "==> Building Flatpak"
flatpak-builder \
    --force-clean \
    --install \
    --user \
    --install-deps-from=flathub \
    --state-dir="${STATE_DIR}" \
    "${BUILD_DIR}" \
    io.github.frazier.Fuse.yml

echo
echo "Done. Run with:"
echo "  flatpak run io.github.frazier.Fuse"
