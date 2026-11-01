#!/usr/bin/env bash
# Start a single-process Tiled 0.2.20 server (port 8000) with a persistent SQLite catalog,
# writable file + DuckDB storage, read access to the detector data, and the in-memory
# streaming cache that backs websocket subscriptions.
#
# Uses `tiled serve config` because `tiled serve catalog --cache ...` without `--temp`
# fails in tiled 0.2.20 (UnboundLocalError: StreamingCacheConfig).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
export PROTOTYPE_ROOT=${PROTOTYPE_ROOT:-/tmp/bluesky-adaptive-prototypes}
export TILED_API_KEY=${TILED_API_KEY:-secret}
export TST_DATA_DIR=${TST_DATA_DIR:-$PROTOTYPE_ROOT/data}
TILED_PORT=${TILED_PORT:-8000}
TILED_DIR=$PROTOTYPE_ROOT/tiled
mkdir -p "$TILED_DIR/data" "$TST_DATA_DIR"

cat > "$TILED_DIR/config.yml" <<YAML
catalog:
  uri: "sqlite:///$TILED_DIR/catalog.db"
  init_if_not_exists: true
  writable_storage:
    - "$TILED_DIR/data"
    - "duckdb:///$TILED_DIR/tabular.duckdb"
  readable_storage:
    - "$TST_DATA_DIR"
streaming_cache:
  uri: memory
YAML

cd "$REPO"
exec pixi run --frozen -e integration tiled serve config "$TILED_DIR/config.yml" \
  --api-key "$TILED_API_KEY" --host 127.0.0.1 --port "$TILED_PORT"
