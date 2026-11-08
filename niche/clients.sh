#!/usr/bin/env bash
# Daily run for client samples / paid clients. Called by .github/workflows/niche-clients.yml.
#
# For every folder  $DATA_DIR/clients/<slug>/  that has stores.txt (same format as a niche
# store list), collect the shops and write a comparison for the client:
#   clients/<slug>/porownanie-<day>.csv   (opens in Google Sheets / Excel)
#   clients/<slug>/podsumowanie-<day>.md
# A folder with a file named "stop" is skipped. Everything stays in the PRIVATE data repo;
# the public log shows only counts.
set -uo pipefail
: "${DATA_DIR:?}" "${DB_DIR:?}"
DAY=$(date -u +%F)
export STOREWATCH_DB="$(mktemp -d)/storewatch.db"
[ -f "$DB_DIR/storewatch.db.gz" ] && gunzip -c "$DB_DIR/storewatch.db.gz" > "$STOREWATCH_DB"
mkdir -p "$DATA_DIR/logs"
LOG="$DATA_DIR/logs/$DAY-clients.log"
N=0
for dir in "$DATA_DIR"/clients/*/; do
  [ -f "$dir/stores.txt" ] || continue
  [ -f "$dir/stop" ] && continue
  [ -f "$dir/porownanie-$DAY.csv" ] && continue      # backup run: already done today
  slug=$(basename "$dir" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
  [ -n "$slug" ] || continue
  while IFS= read -r line; do
    line=${line%%#*}; host=${line%%|*}; host=$(echo "$host" | tr -d '[:space:]')
    host=${host#*://}; host=${host%%/*}; host=${host%%:*}
    [ -n "$host" ] && [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$host"
  done < "$dir/stores.txt"
  market="c-$slug"
  {
    echo "== $(date -u +%FT%TZ) client=$slug"
    python -m storewatch sync "$dir/stores.txt" --market "$market"
    python -m storewatch run --market "$market" --keep 30
    python -m storewatch export --market "$market" --gtin --csv "$dir/.export.csv"
    python niche/client_report.py "$dir/.export.csv" "$dir/porownanie-$DAY.csv" "$dir/podsumowanie-$DAY.md"
    rm -f "$dir/.export.csv"
  } >> "$LOG" 2>&1
  N=$((N+1))
done
gzip -c "$STOREWATCH_DB" > "$DB_DIR/storewatch.db.gz" 2>/dev/null || true
echo "clients run finished: clients=$N (details in the private repository)"
