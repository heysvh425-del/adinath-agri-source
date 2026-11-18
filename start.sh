#!/usr/bin/env bash
# One-command setup + launch for Mac/Linux.
set -e
cd "$(dirname "$0")/backend"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
echo "Installing dependencies (first run only takes a minute)..."
pip install -q -r requirements.txt

echo ""
python run.py
