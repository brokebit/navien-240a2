#!/bin/sh
# Upload the Grafana dashboards through Grafana's HTTP API, replacing earlier copies (same uid).
#
#   GRAFANA_URL=https://grafana.example:3000 GRAFANA_TOKEN=glsa_... scripts/grafana-push.sh
#   scripts/grafana-push.sh path/to/dashboards     # e.g. the JSON files from a release
#   scripts/grafana-push.sh --check                # only report the token's permissions
#
# GRAFANA_TOKEN is a service account token with the Editor role. Instead of the variable,
# the token can be in a file: GRAFANA_TOKEN_FILE (default ~/.config/navien-dash/grafana-token).
# GRAFANA_FOLDER (default "Navien", uid "navien") is created if missing; set it empty for the
# General folder. Dashboards keep their file defaults (data source, gas price). The token is
# passed to curl in a temp file, not on the command line.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CHECK=
[ "${1:-}" = --check ] && { CHECK=1; shift; }
DIR=${1:-$ROOT/grafana/dashboards}
URL=${GRAFANA_URL:?set GRAFANA_URL, e.g. https://grafana.example:3000}
URL=${URL%/}
FOLDER=${GRAFANA_FOLDER-Navien}
FOLDER_UID=navien

TOKEN=${GRAFANA_TOKEN:-}
TOKEN_FILE=${GRAFANA_TOKEN_FILE:-$HOME/.config/navien-dash/grafana-token}
if [ -z "$TOKEN" ] && [ -r "$TOKEN_FILE" ]; then
    TOKEN=$(tr -d ' \n\r' < "$TOKEN_FILE")
fi
[ -n "$TOKEN" ] || { echo "set GRAFANA_TOKEN or put the token in $TOKEN_FILE" >&2; exit 1; }

set -- "$DIR"/navien-*.json
[ -e "$1" ] || { echo "no navien-*.json in $DIR" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
umask 077
printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$TOKEN" > "$TMP/headers"

# api METHOD PATH [BODY_FILE] -> prints the response body, returns the HTTP status in $STATUS
api() {
    if [ $# -ge 3 ]; then
        STATUS=$(curl -sS -o "$TMP/resp" -w '%{http_code}' -X "$1" -H @"$TMP/headers" \
                 --data-binary @"$3" "$URL$2")
    else
        STATUS=$(curl -sS -o "$TMP/resp" -w '%{http_code}' -X "$1" -H @"$TMP/headers" "$URL$2")
    fi
}

# Permissions this script uses; Grafana's Editor role has them all.
NEEDED="folders:read folders:create dashboards:read dashboards:create dashboards:write"
api GET /api/access-control/user/permissions
if [ "$STATUS" = 200 ]; then
    MISSING=$(python3 -c '
import json, sys
have = json.load(open(sys.argv[1]))
print(" ".join(p for p in sys.argv[2].split() if p not in have))' "$TMP/resp" "$NEEDED")
    if [ -n "$MISSING" ]; then
        echo "the token lacks: $MISSING" >&2
        echo "give its service account the Editor role (Administration > Users and access >" >&2
        echo "Service accounts > the account > Role); the token itself need not change" >&2
        exit 1
    fi
    echo "token ok: has $NEEDED"
elif [ "$STATUS" = 401 ]; then
    echo "Grafana did not accept the token (401): $(cat "$TMP/resp")" >&2; exit 1
else
    echo "could not list the token's permissions ($STATUS); trying anyway" >&2
fi
[ -z "$CHECK" ] || exit 0

api GET /api/folders/$FOLDER_UID
case $STATUS in
    401|403) echo "Grafana refused the token ($STATUS): $(cat "$TMP/resp")" >&2; exit 1 ;;
esac
if [ -n "$FOLDER" ]; then
    if [ "$STATUS" = 404 ]; then
        python3 -c 'import json,sys; print(json.dumps({"uid": sys.argv[1], "title": sys.argv[2]}))' \
            "$FOLDER_UID" "$FOLDER" > "$TMP/folder.json"
        api POST /api/folders "$TMP/folder.json"
        [ "$STATUS" = 200 ] || { echo "creating folder failed ($STATUS): $(cat "$TMP/resp")" >&2; exit 1; }
        echo "created folder $FOLDER"
    elif [ "$STATUS" != 200 ]; then
        echo "folder lookup failed ($STATUS): $(cat "$TMP/resp")" >&2; exit 1
    fi
else
    FOLDER_UID=
fi

FAILED=0
for f in "$@"; do
    python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
d["id"] = None                      # match by uid, not by an id from another server
body = {"dashboard": d, "overwrite": True, "message": "scripts/grafana-push.sh"}
if sys.argv[2]:
    body["folderUid"] = sys.argv[2]
print(json.dumps(body))' "$f" "$FOLDER_UID" > "$TMP/body.json"
    api POST /api/dashboards/db "$TMP/body.json"
    if [ "$STATUS" = 200 ]; then
        echo "ok   $(basename "$f") -> $URL$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$TMP/resp")"
    else
        echo "FAIL $(basename "$f") ($STATUS): $(cat "$TMP/resp")" >&2
        FAILED=1
    fi
done
exit $FAILED
