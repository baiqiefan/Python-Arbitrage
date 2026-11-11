#!/usr/bin/env bash
# Sets up the scanner on a fresh Ubuntu server so it starts on boot and restarts after a crash.
# Run it from inside the cloned repository:  bash deploy/setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
sudo apt-get update
sudo apt-get install -y python3 python3-requests

python3 -m unittest 2>&1 | tail -3

sed -e "s|SCANNER_USER|$USER|" -e "s|SCANNER_DIR|$PWD|" deploy/arbitrage-scanner.service \
    | sudo tee /etc/systemd/system/arbitrage-scanner.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now arbitrage-scanner

echo
echo "Scanner is running. Useful commands:"
echo "  journalctl -u arbitrage-scanner -f      watch scans live (Ctrl+C to stop watching)"
echo "  python3 main.py --report                 summarize the data"
echo "  sudo systemctl restart arbitrage-scanner after 'git pull'"
