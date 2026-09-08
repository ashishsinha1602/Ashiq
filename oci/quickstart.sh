#!/usr/bin/env bash
# schemagate against your Autonomous Database, from OCI Cloud Shell.
# No API key: cataloguing uses OCI Generative AI with your own OCI identity.
#
#   bash quickstart.sh 'ADMIN' 'my_db_high' 'the-admin-password'
#
set -euo pipefail

USER_NAME="${1:-ADMIN}"
DSN="${2:-}"
PASSWORD="${3:-}"
MODEL="${SCHEMAGATE_OCI_MODEL:-google.gemini-2.5-pro}"

if [ -z "$DSN" ] || [ -z "$PASSWORD" ]; then
  echo "usage: bash quickstart.sh <user> <tns-alias-or-descriptor> <password>" >&2
  exit 2
fi

echo "==> installing schemagate"
pip install --quiet --user 'schemagate[oracle,oci]'
export PATH="$HOME/.local/bin:$PATH"

export SCHEMAGATE_CONNECT_ARGS="$(python3 - "$USER_NAME" "$PASSWORD" "$DSN" <<'PY'
import json, sys
print(json.dumps({"user": sys.argv[1], "password": sys.argv[2], "dsn": sys.argv[3]}))
PY
)"
URL='oracle+oracledb://@'

echo "==> cataloguing with OCI Generative AI ($MODEL) -- nothing leaves your tenancy"
schemagate describe --url "$URL" --provider oci --model "$MODEL" \
    --all --config catalog.json

echo
echo "==> asking a question"
schemagate select --url "$URL" --config catalog.json \
    "${SCHEMAGATE_QUESTION:-which customers still owe us money}" --prompt

echo
echo "catalog.json holds the descriptions; reuse it with --config on every call."
