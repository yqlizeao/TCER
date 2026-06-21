#!/bin/bash
cd "$(dirname "$0")" || exit 1

# Prefer Homebrew Python (ships with tkinter on macOS)
if command -v python3 &>/dev/null; then
    python3 -m tcer
    exit $?
fi

echo ""
echo "[ERROR] Python 3 not found."
echo "Install with:  brew install python@3"
echo "  or download from https://www.python.org/downloads/"
echo ""
read -n 1 -s -r -p "Press any key to close..."
echo ""
exit 1
