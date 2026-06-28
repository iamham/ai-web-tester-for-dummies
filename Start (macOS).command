#!/bin/bash
# macOS launcher — double-click this file.
# First run installs everything (a few hundred MB); later runs start instantly.
cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "   AI Web Tester"
echo "================================================"

# Make sure uv (the installer/runtime manager) is available.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing the runtime manager (uv)… one-time."
  curl -LsSf https://astral.sh/uv/install.sh | sh || {
    echo "Could not install uv. Check your internet connection and try again."
    read -r -p "Press Enter to close."; exit 1; }
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

# One-time install of Python deps + Chromium.
if [ ! -d ".venv" ]; then
  echo "First-time setup — installing Python, dependencies and a browser."
  echo "This can take several minutes. Please wait…"
  uv venv --python 3.12 || { echo "Setup failed (venv)."; read -r -p "Press Enter to close."; exit 1; }
  uv pip install -r requirements.txt || { echo "Setup failed (deps)."; read -r -p "Press Enter to close."; exit 1; }
  .venv/bin/python -m browser_use install || {
    echo "Setup failed (browser)."; read -r -p "Press Enter to close."; exit 1; }
fi

# Create a local settings file on first run (never overwrites an existing one).
[ -f .env ] || cp .env.example .env

echo "Starting… your browser will open automatically."
.venv/bin/python app.py
read -r -p "Server stopped. Press Enter to close."
