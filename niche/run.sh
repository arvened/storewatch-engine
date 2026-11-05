#!/usr/bin/env bash
# One daily run of one niche. Called by .github/workflows/niche.yml.
#
# The public log of this repository must never show a store address, a product or a price.
# So: store addresses come only from the NICHE_STORES secret, every host is masked in the log,
# and ALL program output goes to a log file inside the PRIVATE data repository (DATA_DIR).
# The public log shows only counts and the exit code.
#
# Inputs (environment):
#   NICHE_STORES  store list, same format as markets/*.txt   (secret)
#   NICHE         market code, e.g. pl-equip                  (secret or plain)
#   DATA_DIR      checkout of the private data repository
#   DB_DIR        checkout of the private repository's "db" branch (database between runs)
set -uo pipefail

: "${NICHE_STORES:?NICHE_STORES secret is empty}"
: "${NICHE:?NICHE is empty}"
: "${DATA_DIR:?}" "${DB_DIR:?}"
# Tolerate how a secret was typed on a phone: spaces, line breaks, capitals.
NICHE=$(printf '%s' "$NICHE" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
case "$NICHE" in *[!a-z0-9_-]*|"") echo "NICHE must be a-z 0-9 _ -"; exit 2;; esac

DAY=$(date -u +%F)
WORK=$(mktemp -d)
STORES="$WORK/stores.txt"
printf '%s\n' "$NICHE_STORES" > "$STORES"

# Hide every store host from the public log, just in case something slips through.
while IFS= read -r line; do
  line=${line%%#*}; host=${line%%|*}; host=$(echo "$host" | tr -d '[:space:]')
  host=${host#*://}; host=${host%%/*}; host=${host%%:*}
  [ -n "$host" ] && [ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$host"
done < "$STORES"

mkdir -p "$DATA_DIR/logs" "$DATA_DIR/reports/$NICHE" "$DATA_DIR/history/$NICHE" "$DB_DIR"
LOG="$DATA_DIR/logs/$DAY-$NICHE.log"
export STOREWATCH_DB="$WORK/storewatch.db"
[ -f "$DB_DIR/storewatch.db.gz" ] && gunzip -c "$DB_DIR/storewatch.db.gz" > "$STOREWATCH_DB"

STATUS=0
(
  echo "== $(date -u +%FT%TZ) niche=$NICHE"
  python -m storewatch sync "$STORES" --market "$NICHE"
  python -m storewatch run --market "$NICHE" --keep 30 \
    --md "$DATA_DIR/reports/$NICHE/$DAY.md" --json "$DATA_DIR/reports/$NICHE/latest.json" \
    --usage "$DATA_DIR/usage.csv"
  code=$?
  echo "run exit code: $code"
  if [ -z "$(ls -A "$DATA_DIR/history/$NICHE")" ]; then
    python -m storewatch export --market "$NICHE" --all --csv "$DATA_DIR/history/$NICHE/backfill-$DAY.csv.gz"
  else
    python -m storewatch export --market "$NICHE" --csv "$DATA_DIR/history/$NICHE/$DAY.csv.gz"
  fi
  [ -f "$DATA_DIR/reports/$NICHE/$DAY.md" ] && cp "$DATA_DIR/reports/$NICHE/$DAY.md" "$DATA_DIR/reports/$NICHE/latest.md"
  exit "$code"
) >> "$LOG" 2>&1
code=$?
# 0 = all stores read; 4 = some stores unreadable (listed in the private report). Both are normal.
if [ "$code" -ne 0 ] && [ "$code" -ne 4 ]; then STATUS=$code; fi

gzip -c "$STOREWATCH_DB" > "$DB_DIR/storewatch.db.gz" 2>/dev/null || true
N=$(grep -cvE '^\s*(#|$)' "$STORES" || true)
rm -rf "$WORK"

# The only lines that reach the public log:
echo "niche run finished: stores=$N exit=$code (details in the private repository, logs/)"
exit $STATUS
