#!/usr/bin/env bash
# Build a SalvageMap .deb — a native Ubuntu installer that pulls PySide6,
# ddrescue and ntfs-3g from apt, so testers install with a double-click (or
# `sudo apt install ./salvagemap_*.deb`) and never touch pip or a build step.
#
# Pure-Python, architecture-independent (arch: all). Targets Ubuntu 24.04+,
# whose apt provides the python3-pyside6.* packages and Python >= 3.11.
#
# Usage:  packaging/build-deb.sh [VERSION]
# VERSION defaults to the version in pyproject.toml. Output: dist/salvagemap_<ver>_all.deb
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
root="$(dirname "$here")"
deb="$here/deb"

# --- version ---------------------------------------------------------------
version="${1:-}"
if [ -z "$version" ]; then
    version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -n1)"
fi
if [ -z "$version" ]; then
    echo "error: could not determine version" >&2
    exit 1
fi
echo "Building salvagemap $version"

# --- staging tree ----------------------------------------------------------
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

install -d "$stage/DEBIAN"
install -d "$stage/usr/lib/salvagemap"
install -d "$stage/usr/bin"
install -d "$stage/usr/share/applications"
install -d "$stage/usr/share/icons/hicolor/scalable/apps"
install -d "$stage/usr/share/doc/salvagemap"

# App package + resources under /usr/lib/salvagemap (main.py finds the icon at
# ../resources/salvagemap.svg relative to the app package, so keep that layout).
cp -r "$root/app" "$stage/usr/lib/salvagemap/app"
cp -r "$root/resources" "$stage/usr/lib/salvagemap/resources"
# Drop compiled-bytecode caches from the copied tree.
find "$stage/usr/lib/salvagemap" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$stage/usr/lib/salvagemap" -name '*.py[co]' -delete

# Launchers.
install -m 0755 "$deb/salvagemap"          "$stage/usr/bin/salvagemap"
install -m 0755 "$deb/salvagemap-pkexec"   "$stage/usr/bin/salvagemap-pkexec"

# Desktop entry + icon (menu integration).
install -m 0644 "$deb/salvagemap.desktop"  "$stage/usr/share/applications/salvagemap.desktop"
install -m 0644 "$root/resources/salvagemap.svg" \
        "$stage/usr/share/icons/hicolor/scalable/apps/salvagemap.svg"

# Docs / copyright (Debian policy).
install -m 0644 "$deb/copyright"           "$stage/usr/share/doc/salvagemap/copyright"

# Control + maintainer scripts.
sed "s/@VERSION@/$version/" "$deb/control.in" > "$stage/DEBIAN/control"
install -m 0755 "$deb/postinst"            "$stage/DEBIAN/postinst"
install -m 0755 "$deb/postrm"              "$stage/DEBIAN/postrm"

# --- build -----------------------------------------------------------------
mkdir -p "$root/dist"
out="$root/dist/salvagemap_${version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$out"
echo "Wrote $out"
dpkg-deb --info "$out" | sed -n '1,20p'
