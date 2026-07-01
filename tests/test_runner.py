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

import pytest

from app.core.ddrescue_runner import (
    RescueSettings,
    SafetyError,
    build_command,
    parse_status_line,
    validate_targets,
)


def test_build_command_basic():
    argv = build_command("/dev/sdb", "out.img", "out.map", RescueSettings())
    assert argv[0] == "ddrescue"
    assert "--sector-size=512" in argv
    assert "--mapfile-interval=1" in argv
    assert argv[-3:] == ["/dev/sdb", "out.img", "out.map"]


def test_build_command_domain_and_options():
    s = RescueSettings(
        reverse=True,
        retry_passes=3,
        domain_mapfile="idx.dmap",
        loose_domain=True,
        sector_size=4096,
    )
    argv = build_command("/dev/sdb", "out.img", "out.map", s)
    assert "--reverse" in argv
    assert "--retry-passes=3" in argv
    assert "--domain-mapfile=idx.dmap" in argv
    assert "--loose-domain" in argv
    assert "--sector-size=4096" in argv


def test_validate_rejects_same_target(tmp_path):
    dev = tmp_path / "img"
    dev.write_bytes(b"x")
    with pytest.raises(SafetyError):
        validate_targets(str(dev), str(dev))


def test_validate_allows_new_output(tmp_path):
    src = tmp_path / "src"
    src.write_bytes(b"x")
    validate_targets(str(src), str(tmp_path / "new.img"))  # no raise


def test_parse_status_line():
    text = (
        "     ipos:    1024 B, non-trimmed:        0 B,  current rate:   512 B/s\n"
        "  rescued:    2048 B,   bad areas:        0,        run time:      1s\n"
        "pct rescued:  100.00%, read errors:        0\n"
    )
    fields = parse_status_line(text)
    assert fields["rescued"] == "2048 B"
    assert fields["pct rescued"] == "100.00%"
    assert fields["bad areas"] == "0"
    assert fields["read errors"] == "0"


def test_failing_drive_command_flags():
    from app.core.ddrescue_runner import failing_drive_settings, build_command
    s = failing_drive_settings()
    argv = build_command("/dev/sdc", "out.img", "out.log", s)
    assert "--no-trim" in argv
    assert "--no-scrape" in argv
    assert "--timeout=30s" in argv
    assert any(a.startswith("--skip-size=") for a in argv)


def test_filesystem_type_resolves_root():
    from app.core.ddrescue_runner import filesystem_type
    # The root mount always exists and has a type.
    assert filesystem_type("/") is not None


def test_non_sparse_destination_logic(monkeypatch):
    import app.core.ddrescue_runner as r
    # Pretend the destination dir lives on exfat.
    monkeypatch.setattr(r, "filesystem_type", lambda p: "exfat")
    assert r.non_sparse_destination("/whatever/out.img") == "exfat"
    monkeypatch.setattr(r, "filesystem_type", lambda p: "ext4")
    assert r.non_sparse_destination("/whatever/out.img") is None
