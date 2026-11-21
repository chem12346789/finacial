#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$ROOT/broker/.venv/bin/python}"

if (( $# > 0 )) && [[ "$1" != -* ]]; then
    AS_OF="$1"
    shift
    exec "$PYTHON" "$ROOT/play_money_orders.py" --order-type LIMIT --as-of "$AS_OF" "$@"
fi

exec "$PYTHON" "$ROOT/play_money_orders.py" --order-type LIMIT "$@"
