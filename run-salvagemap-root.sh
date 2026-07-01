#!/usr/bin/env bash
# Launch SalvageMap with root privileges (needed to read raw block devices) from
# a desktop icon. When run as a normal user this re-execs itself through pkexec,
# which pops the graphical polkit password prompt.
#
# The GUI still has to reach the display. pkexec scrubs the environment, so we
# forward it explicitly. We prefer the *native* Wayland platform so the app
# looks identical to the normal (non-root) launch — root bypasses the file
# permissions on the user's Wayland socket, so it can connect once
# WAYLAND_DISPLAY + XDG_RUNTIME_DIR are passed through. We fall back to XWayland
# (xcb) via `QT_QPA_PLATFORM=wayland;xcb`, and authorise root to the X server so
# that fallback works on both X11 and Wayland sessions.
#
# Prefer NOT running the whole GUI as root: adding yourself to the `disk` group
# (`sudo usermod -aG disk $USER`, then log out/in) lets the normal launcher
# `run-salvagemap.sh` read devices without any of this. Use this script only if
# you specifically want the sudo/desktop-icon flow.

here="$(dirname "$(readlink -f "$0")")"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    # Authorise root to talk to the (X)Wayland display, remembering whether we
    # actually granted it so we can revoke exactly that afterwards.
    granted=0
    if command -v xhost >/dev/null 2>&1; then
        if xhost +SI:localuser:root >/dev/null 2>&1; then
            granted=1
        fi
    fi

    pkexec env \
        DISPLAY="${DISPLAY:-:0}" \
        XAUTHORITY="${XAUTHORITY:-}" \
        WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
        XDG_CURRENT_DESKTOP="${XDG_CURRENT_DESKTOP:-}" \
        DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
        QT_QPA_PLATFORM="wayland;xcb" \
        "$here/run-salvagemap-root.sh" "$@"
    status=$?

    if [ "$granted" -eq 1 ]; then
        xhost -SI:localuser:root >/dev/null 2>&1 || true
    fi
    exit "$status"
fi

# Now running as root: launch the app from the project directory so the `app`
# package is importable without installing anything.
cd "$here" || exit 1
exec python3 -m app.main "$@"
