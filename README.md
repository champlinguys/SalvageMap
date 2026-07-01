# SalvageMap

> ⚠️ **No warranty — use at your own risk.** This tool reads from failing
> storage. Any read activity against a dying drive can hasten its failure, and
> recovery is never guaranteed. **Always work on a healthy spare drive, never
> the customer's original, and image to a separate target.** The source device
> is always opened read-only and the app refuses to use a block device or the
> source itself as the output target, but you remain responsible for selecting
> the correct devices. This software is provided "as is", without warranty of
> any kind (see [LICENSE](LICENSE)).

A Linux GUI wrapper over [GNU ddrescue](https://www.gnu.org/software/ddrescue/),
in the style of FTK / DMDE / Data Extractor:

- **Live sector map** — the ddrescue mapfile rendered as a grid of tall coloured
  rectangles (green = finished, red = bad sector, …), updating as a rescue runs.
- **Targeted recovery** — for a failing drive that will never image 100%, read
  in *priority order* and extract the most valuable data first. **ddrescue is the
  only thing that ever reads the failing device; every structure is parsed from
  the output image.** A filesystem-agnostic engine images the partition table and
  each partition's boot record, **detects the filesystem from the image**, then
  walks a per-filesystem plan. Currently supported:

  | Filesystem | Metadata imaged in priority order | File data |
  | --- | --- | --- |
  | **NTFS** | boot sector → `$MFT` record 0 (own runs) → full `$MFT` → every directory's `$INDEX_ALLOCATION` | all allocated `$DATA` (resident small files already in the `$MFT`) |
  | **ext4** | superblock → group descriptor table → every inode table → every directory's data blocks | every regular file's extents (ext3/ext2 indirect-block files are counted but skipped) |
  | **HFS+** | volume header → Catalog B-tree → Extents Overflow file | every file's data-fork extents (compressed/resource-fork files are counted but skipped) |

  Each phase runs `ddrescue --domain-mapfile` into the same image + logfile, so
  the sector map fills in cumulatively. Free space is always skipped. **File ▸
  Export file-data Domain File** writes the best domain file so you can re-run
  `ddrescue -m` manually with your own settings.

## Requirements

- Python 3.11+
- PySide6 (Qt 6)
- `ddrescue` (1.20+; tested with 1.30) on `PATH`
- For tests: `pytest`, plus the filesystem tools used by the integration checks
  (`ntfs-3g` / `mkntfs` for NTFS, `e2fsprogs` / `mke2fs` for ext4, and
  `hfsprogs` for HFS+)

On Debian/Ubuntu:

```sh
sudo apt-get install gddrescue python3-pyside6.qtwidgets python3-pyside6.qtgui \
                     python3-pyside6.qtcore python3-pytest ntfs-3g
```

## Run

```sh
python3 -m app.main
```

Then **File ▸ Open Device…** to choose the source device and an output image,
and **Options ▸ Targeted Recovery ▸ Run full workflow**. The filesystem (NTFS,
ext4, or HFS+) is detected automatically from the imaged partition. Use **File ▸
Open Mapfile** to view any existing ddrescue mapfile read-only.

> The source is always opened read-only by ddrescue; the app refuses to use a
> block device or the source itself as the output target.

While a rescue is running, the **Recovered files** pane stays empty — parsing
the `$MFT` and colouring every file against the mapfile is too heavy to do live.
Click **Show Files** in the toolbar to pause the rescue (progress is saved to
the logfile) and browse the recovered filesystem with per-file recovery status,
then **Resume** to continue ddrescue from where it left off.

## Layout

```
app/
  main.py                     entry point
  ui/        main_window, sector_map, status_panel, log_panel
  core/      mapfile (parse/aggregate), domain (domain-mapfile builder),
             ddrescue_runner (QProcess wrapper + safety guards)
  ntfs/      runlist, boot_sector, mft, targeted_recovery (4-phase orchestrator)
tests/       unit tests + sample mapfile
```

## Tests

```sh
python3 -m pytest -q
```

## License

Copyright (C) 2026 Champlin Guys Data Recovery.

This project is licensed under the **GNU General Public License v3.0 or later**
(GPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

## Acknowledgements

This app does not contain, link, or bundle any of the software below — it
invokes `ddrescue` as a separate command-line process and depends on PySide6 as
an external library. They are credited here with gratitude:

- **[GNU ddrescue](https://www.gnu.org/software/ddrescue/)** by Antonio Diaz
  Diaz — the data-recovery engine that does all reading from the source device.
  Licensed under the GPLv3. Install it yourself (`apt-get install gddrescue`);
  it is not distributed with this project.
- **[Qt for Python (PySide6)](https://wiki.qt.io/Qt_for_Python)** — the GUI
  toolkit, licensed under the LGPLv3.
