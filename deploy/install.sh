#!/bin/sh
# Install Arrgus as a systemd user service for the current user.
# Usage: deploy/install.sh config/<box>.toml
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CONFIG=$(realpath "${1:?usage: $0 config/<box>.toml}")
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s|@ROOT@|$ROOT|g" -e "s|@CONFIG@|$CONFIG|g" -e "s|@HOME@|$HOME|g" \
    "$ROOT/deploy/arrgus.service" > "$UNIT_DIR/arrgus.service"
python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "$CONFIG"
# Start at boot without an interactive login (allowed for your own user by default polkit policy).
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  loginctl enable-linger "$USER" || echo "WARNING: could not enable lingering; run: sudo loginctl enable-linger $USER"
fi
systemctl --user daemon-reload
systemctl --user enable arrgus.service
systemctl --user restart arrgus.service
sleep 2
systemctl --user --no-pager status arrgus.service | head -5
