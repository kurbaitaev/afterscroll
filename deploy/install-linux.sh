#!/bin/bash
# Install Afterscroll on a Linux VPS as systemd --user units:
#   the bot (always on), the watchdog (every 90 min), the digest (Sunday 18:00),
#   remake drafts (every 15 min) and reel views (every 3 days).
# Run as the normal user that owns the checkout, from anywhere:
#   ./deploy/install-linux.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer the project venv — a VPS without sudo can't apt-install anything, and
# the system python must not be polluted.
PYTHON="${PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -f "$HERE/.env" ]; then
  echo "error: no .env yet. Run:  cp .env.example .env  then fill it in." >&2
  exit 1
fi
if ! grep -qE '^(ANTHROPIC_API_KEY|OPENAI_API_KEY)=.+' "$HERE/.env" \
   && ! grep -q '"claudeAiOauth"' "$HOME/.claude/.credentials.json" 2>/dev/null; then
  echo "error: no reasoning backend. Either put ANTHROPIC_API_KEY (or OPENAI_API_KEY)" >&2
  echo "       in .env, or log the Claude CLI in on this machine (claude → /login)." >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo "error: systemd not found." >&2; exit 1; }

# One-time move from the old unit names (saved-to-notes*, before 2026-11-19).
for old in $(ls "$UNIT_DIR" 2>/dev/null | grep -E '^saved-to-notes.*\.(service|timer)$'); do
  systemctl --user disable --now "$old" 2>/dev/null || true
  mkdir -p "$UNIT_DIR/old-units" && mv "$UNIT_DIR/$old" "$UNIT_DIR/old-units/$old"
done
systemctl --user daemon-reload
mkdir -p "$HERE/logs" "$UNIT_DIR" /tmp/saved-to-notes

render() { sed -e "s|__DIR__|$HERE|g" -e "s|__PYTHON__|$PYTHON|g" "$1"; }

for unit in afterscroll.service \
            afterscroll-watchdog.service afterscroll-watchdog.timer \
            afterscroll-digest.service   afterscroll-digest.timer \
            afterscroll-vaultsync.service afterscroll-vaultsync.timer \
            afterscroll-content-drafts.service afterscroll-content-drafts.timer \
            afterscroll-content-stats.service afterscroll-content-stats.timer \
            afterscroll-xfeed.service afterscroll-xfeed.timer \
            afterscroll-api.service; do
  render "$HERE/deploy/$unit" > "$UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now afterscroll.service
systemctl --user enable --now afterscroll-watchdog.timer
systemctl --user enable --now afterscroll-digest.timer
systemctl --user enable --now afterscroll-vaultsync.timer
systemctl --user enable --now afterscroll-content-drafts.timer
systemctl --user enable --now afterscroll-content-stats.timer
systemctl --user enable --now afterscroll-xfeed.timer
# The app's API needs a token; make one the first time (never printed).
grep -q '^APP_TOKEN=.' "$HERE/.env" 2>/dev/null \
  || echo "APP_TOKEN=$("$PYTHON" -c 'import secrets;print(secrets.token_urlsafe(32))')" >> "$HERE/.env"
systemctl --user enable --now afterscroll-api.service
systemctl --user restart afterscroll-api.service

# Without linger the units stop the moment the SSH session ends.
if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q "Linger=yes"; then
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || echo "NOTE: linger is off and needs root once:  sudo loginctl enable-linger $(id -un)"
fi

echo
systemctl --user --no-pager status afterscroll.service | head -5
systemctl --user --no-pager list-timers 'afterscroll-*' | head -5
echo
echo "  systemctl --user restart afterscroll    # after git pull"
echo "  journalctl --user -u afterscroll -f     # live logs"
