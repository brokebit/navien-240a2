#!/bin/sh
# Publish a GitHub release for a tag, with its .deb attached.
#
#   scripts/release.sh            release the vX.Y.Z tag at HEAD
#   scripts/release.sh v0.4.1     release that tag
#
# Needs `gh` logged in. The tag must already be on GitHub and the package already built
# (scripts/build-deb.sh at that tag). Release notes are the tagged commit's message plus
# install instructions. The Grafana dashboards (grafana/dashboards/*.json at the tag) are
# attached too. Re-running for an existing release replaces the attached files.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

TAG=${1:-$(git describe --tags --exact-match --match 'v[0-9]*' HEAD 2>/dev/null || true)}
if [ -z "$TAG" ]; then
    echo "HEAD is not at a vX.Y.Z tag; pass the tag to release" >&2
    exit 1
fi
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "no tag $TAG" >&2; exit 1; }

VERSION=${TAG#v}
ARCH=$(dpkg --print-architecture)
DEB="dist/navien-dash_${VERSION}_${ARCH}.deb"
[ -f "$DEB" ] || { echo "$DEB not found; build it at $TAG first" >&2; exit 1; }
PKGVER=$(dpkg-deb -f "$DEB" Version)
[ "$PKGVER" = "$VERSION" ] || { echo "$DEB has version $PKGVER, expected $VERSION" >&2; exit 1; }

# The tag on GitHub must be the same commit as ours (gh would otherwise create it).
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
LOCAL=$(git rev-list -n 1 "$TAG")
REMOTE=$(gh api "repos/$REPO/commits/$TAG" --jq .sha 2>/dev/null || true)
if [ "$REMOTE" != "$LOCAL" ]; then
    echo "tag $TAG is not on GitHub at $LOCAL; push tags first (git push origin --tags)" >&2
    exit 1
fi

# Grafana dashboards as they are at the tag, not in the working tree.
ASSETS=$(mktemp -d)
NOTES="$ASSETS/notes.md"
trap 'rm -rf "$ASSETS"' EXIT
for f in $(git ls-tree --name-only "$TAG" grafana/dashboards/ | grep '\.json$' || true); do
    git show "$TAG:$f" > "$ASSETS/$(basename "$f")"
done
set -- "$DEB"
for f in "$ASSETS"/*.json; do
    [ -e "$f" ] && set -- "$@" "$f"
done

if gh release view "$TAG" >/dev/null 2>&1; then
    echo "release $TAG exists; replacing its files"
    gh release upload "$TAG" "$@" --clobber
    exit 0
fi
SUBJECT=$(git log -1 --format=%s "$TAG" | sed -E "s/ \($TAG\)$//")
BODY=$(git log -1 --format=%b "$TAG" | sed '/^Co-Authored-By:/d')   # $() drops trailing blank lines
{
    printf '%s\n' "$BODY"
    cat <<EOF

## Install

Raspberry Pi (arm64), Ubuntu with Python $(dpkg-deb -f "$DEB" Depends | sed -nE 's/.*python3 \(>= ([0-9.]+)\).*/\1/p'):

\`\`\`sh
sudo apt install ./$(basename "$DEB")
\`\`\`

Upgrades keep \`/etc/navien-dash/navien.toml\` and the raw logs in \`/var/lib/navien-dash\`.
EOF
    if [ $# -gt 1 ]; then
        printf '\nThe `navien-*.json` files are the Grafana dashboards; setup in `grafana/README.md`.\n'
    fi
} > "$NOTES"

gh release create "$TAG" "$@" --verify-tag --title "$TAG — $SUBJECT" --notes-file "$NOTES"
