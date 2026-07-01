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

"""Back-compat shim.

The targeted-recovery engine is now filesystem-agnostic and lives in
:mod:`app.core.recovery`; the NTFS-specific parse helpers and phase plan live in
:mod:`app.ntfs.plan`. This module re-exports the names existing callers and tests
import (``from app.ntfs.targeted_recovery import Phase, TargetedRecovery`` etc.).
"""

from app.core.recovery import (  # noqa: F401
    Phase,
    RecoveryContext,
    RecoveryState,
    TargetedRecovery,
    get_source_size,
    read_image,
)
from app.ntfs.plan import (  # noqa: F401
    PHASE_STEPS,
    assemble_mft,
    build_filedata_domain,
    collect_filedata_ranges,
    collect_index_ranges,
    load_boot,
    load_mft_ranges,
)
