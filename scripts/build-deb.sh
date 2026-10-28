#!/bin/sh
# Build dist/navien-dash_<version>_<arch>.deb from the current checkout.
#
#   scripts/build-deb.sh          release build: tree must be clean; version from the
#                                 latest vX.Y.Z tag (plus +gitN.gHASH if HEAD is past it)
#   scripts/build-deb.sh --dev    allow uncommitted changes (version gets +dirty)
#
# The package bundles a virtualenv (requirements.lock, installed --no-deps) built against
# this host's python3, so build on the target architecture and Python minor version.
set -eu
umask 022

DEV=0
[ "${1:-}" = "--dev" ] && DEV=1

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

# ---------------------------------------------------------------- version
if [ -n "$(git status --porcelain)" ] && [ "$DEV" -eq 0 ]; then
    echo "working tree is dirty; commit first or use --dev" >&2
    exit 1
fi
DESC=$(git describe --tags --match 'v[0-9]*' --dirty 2>/dev/null || true)
if [ -z "$DESC" ]; then
    DESC="v0.0.0-$(git rev-list --count HEAD)-g$(git rev-parse --short HEAD)"
    [ -n "$(git status --porcelain)" ] && DESC="$DESC-dirty"
fi
# v0.1.0 -> 0.1.0 ; v0.1.0-3-gabc1234 -> 0.1.0+git3.gabc1234 ; ...-dirty -> ...+dirty
VERSION=$(echo "$DESC" | sed -E -e 's/^v//' \
    -e 's/-([0-9]+)-(g[0-9a-f]+)/+git\1.\2/' -e 's/-dirty$/+dirty/' -e 's/\+git(.*)\+dirty$/+git\1.dirty/')
ARCH=$(dpkg --print-architecture)
PY=python3
PYVER=$($PY -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
PYNEXT=$($PY -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1] + 1}")')

NAME="navien-dash_${VERSION}_${ARCH}"
STAGE="$ROOT/build/$NAME"
OPT="$STAGE/opt/navien-dash"
echo "building $NAME (python $PYVER)"
rm -rf "$STAGE"
mkdir -p "$OPT/app" "$ROOT/dist"

# ---------------------------------------------------------------- application
cp -r navien "$OPT/app/"
find "$OPT/app" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------- virtualenv
$PY -m venv "$OPT/venv"
"$OPT/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
    --no-deps -r requirements.lock
"$OPT/venv/bin/python" -m pip check
"$OPT/venv/bin/python" -m pip uninstall --quiet -y pip
echo "/opt/navien-dash/app" > "$OPT/venv/lib/python$PYVER/site-packages/navien-dash.pth"
# Console scripts carry the staging path in their shebang; only `python` is used.
find "$OPT/venv/bin" -mindepth 1 ! -name 'python*' -exec rm -f {} +
find "$OPT" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------- smoke test
# Import everything and replay a capture through the staged venv (app on PYTHONPATH,
# since the .pth points at the final install path).
PYTHONPATH="$OPT/app" PYTHONDONTWRITEBYTECODE=1 "$OPT/venv/bin/python" -c \
    'import navien.api, navien.service, uvicorn, serial; print("imports ok")'
CAPTURE=$(ls captures/*.raw 2>/dev/null | head -1 || true)
if [ -n "$CAPTURE" ]; then
    CYCLES=$(PYTHONPATH="$OPT/app" PYTHONDONTWRITEBYTECODE=1 "$OPT/venv/bin/python" -m navien \
        --headless --replay "$CAPTURE" --speed 0 2>/dev/null | wc -l)
    [ "$CYCLES" -gt 10 ] || { echo "smoke replay produced $CYCLES cycles" >&2; exit 1; }
    echo "replay ok ($CYCLES cycles from $CAPTURE)"
fi

# ---------------------------------------------------------------- system files
install -D -m 0755 /dev/stdin "$STAGE/usr/bin/navien-dash" <<'EOF'
#!/bin/sh
# navien-dash [--headless] [--replay FILE] ...   (see --help)
exec /opt/navien-dash/venv/bin/python -m navien "$@"
EOF
install -D -m 0644 packaging/navien-dash.service "$STAGE/usr/lib/systemd/system/navien-dash.service"
install -D -m 0644 packaging/99-navien-dash.rules "$STAGE/usr/lib/udev/rules.d/99-navien-dash.rules"
install -D -m 0644 packaging/navien-dash.sysusers "$STAGE/usr/lib/sysusers.d/navien-dash.conf"
install -D -m 0644 packaging/navien.toml "$STAGE/etc/navien-dash/navien.toml"
DOC="$STAGE/usr/share/doc/navien-dash"
install -D -m 0644 PROTOCOL_NOTES.md "$DOC/PROTOCOL_NOTES.md"
install -D -m 0644 navien.example.toml "$DOC/navien.example.toml"
echo "$DESC $(git rev-parse HEAD)" > "$DOC/BUILD"

# ---------------------------------------------------------------- DEBIAN
mkdir -p "$STAGE/DEBIAN"
for f in postinst prerm postrm; do
    install -m 0755 "packaging/DEBIAN/$f" "$STAGE/DEBIAN/$f"
done
install -m 0644 packaging/DEBIAN/conffiles "$STAGE/DEBIAN/conffiles"
SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@PYMIN@/$PYVER/" \
    -e "s/@PYNEXT@/$PYNEXT/" -e "s/@SIZE@/$SIZE/" \
    packaging/DEBIAN/control.in > "$STAGE/DEBIAN/control"

# cp -r keeps source modes, which may be group-writable (umask 002); root-owned files must not be.
chmod -R go-w "$STAGE"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "dist/$NAME.deb" >/dev/null
rm -rf "$STAGE"
ls -l "dist/$NAME.deb"
echo "install: sudo apt install ./dist/$NAME.deb"
