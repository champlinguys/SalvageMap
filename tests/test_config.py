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

"""Persisted-config store: best-effort JSON, survives bad input."""

import app.core.config as config


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(config, "_APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_CONFIG_PATH", str(cfg))

    assert config.load() == {}            # first run: no file
    config.save({"sector_size": 4096})
    assert config.load()["sector_size"] == 4096

    config.save({"other": 1})             # merges, doesn't clobber
    loaded = config.load()
    assert loaded == {"sector_size": 4096, "other": 1}


def test_load_tolerates_garbage(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("not json {{{")
    monkeypatch.setattr(config, "_CONFIG_PATH", str(cfg))
    assert config.load() == {}
