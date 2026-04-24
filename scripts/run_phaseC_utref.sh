#!/usr/bin/env bash
# Reproduce RQ3 Phase C — UTRefactor (Gao et al. 2024) baseline.
# UTRefactor is third-party and is NOT bundled with this package. Clone it
# from the upstream repo and point $UTREFACTOR_ROOT at the local checkout.
# See ../../07_environment/ant_build_requirements.md for the exact version.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."

: "${UTREFACTOR_ROOT:?Set UTREFACTOR_ROOT to your local UTRefactor checkout}"

python3 "$HERE/phaseC_run_utrefactor_parallel.py" \
    --utrefactor_root "$UTREFACTOR_ROOT" \
    --cohort 01_cohort/selected_15.csv \
    --out output/rq3/utrefactor \
    "$@"
