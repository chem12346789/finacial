#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$ROOT/broker/.venv/bin/python}"

exec "$PYTHON" "$ROOT/core_asset_risk_parity.py" "$@"
