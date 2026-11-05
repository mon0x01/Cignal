#!/usr/bin/env bash
# Strict local integration gate; never deletes/recreates persistent application data.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/scripts/verify_acquisition_intelligence_v1.sh" ]]; then
  exec bash "$ROOT/scripts/verify_acquisition_intelligence_v1.sh"
fi
if [[ -f "$ROOT/scripts/verify_acquisition_operations_v1.sh" ]]; then
  exec bash "$ROOT/scripts/verify_acquisition_operations_v1.sh"
fi
if [[ -f "$ROOT/scripts/verify_data_acquisition_v1.sh" ]]; then
  exec bash "$ROOT/scripts/verify_data_acquisition_v1.sh"
fi
printf 'FAIL: No Cignal verification script is installed.\n' >&2
exit 1
