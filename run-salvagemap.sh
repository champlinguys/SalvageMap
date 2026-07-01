#!/usr/bin/env bash
# Launch SalvageMap (the ddrescue GUI). Runs from the project directory so the
# `app` package is importable without installing anything.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 -m app.main "$@"
