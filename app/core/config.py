# SalvageMap — GUI wrapper over GNU ddrescue.
# Copyright (C) 2026 Champlin Guys Data Recovery
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Tiny persisted-config store (user-level JSON).

We only persist the handful of values worth surviving a restart — notably the
``sector_size`` the targeted workflow *discovered* on a failing drive (via
unaligned-read escalation), so re-running after a power-cycle doesn't repeat the
slow 512 B failure. Best-effort: any I/O or parse error degrades to defaults.
"""

from __future__ import annotations

import json
import os

_APP_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "salvagemap",
)
_CONFIG_PATH = os.path.join(_APP_DIR, "config.json")


def config_path() -> str:
    return _CONFIG_PATH


def load() -> dict:
    """Return the saved config dict (empty on any error / first run)."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(values: dict) -> None:
    """Merge ``values`` into the saved config. Best-effort (never raises)."""
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
        current = load()
        current.update(values)
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2)
        os.replace(tmp, _CONFIG_PATH)
    except OSError:
        pass
