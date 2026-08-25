#!/usr/bin/env bash
set -euo pipefail

# Create virtual environment if it doesn't exist
if [ ! -d "./.venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
./.venv/bin/python -m pip install -r requirements.txt

echo "Setup complete!"
echo "To use this pipeline:"
echo "  source .venv/bin/activate"
echo "  python ingest.py --inbox"
echo "Configure GEMINI_API_KEY or an ignored local key file before running."
