#!/bin/bash
# Double-click to open your Claude export in the browser.
# The first time, macOS may block it: right-click the file → Open → Open.
cd "$(dirname "$0")" || exit 1
python3 viewer.py "$@"
