#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. On Linux Mint run:"
  echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Fill in DISCORD_TOKEN and FACEIT_API_KEY before starting."
fi

if [[ ! -f players.yaml ]]; then
  cp players.example.yaml players.yaml
fi

mkdir -p data "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/csprogresstracker.service" <<EOF
[Unit]
Description=CS Progress Tracker Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/.venv/bin/python -m bot.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now csprogresstracker.service

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
fi

echo
echo "Bot service installed and started."
echo "  status:  systemctl --user status csprogresstracker"
echo "  logs:    journalctl --user -u csprogresstracker -f"
echo "  restart: systemctl --user restart csprogresstracker"
echo
echo "If .env still has empty tokens, edit it, then:"
echo "  systemctl --user restart csprogresstracker"
