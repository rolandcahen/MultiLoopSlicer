#!/bin/bash
# Construit une application macOS autonome : dist/MultiLoop Slicer.app
# (à lancer sur un Mac, après une première exécution du lanceur .command)
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv; .venv/bin/pip install -r requirements.txt; }
.venv/bin/pip install -q pyinstaller
.venv/bin/pyinstaller --noconfirm --windowed --name "MultiLoop Slicer" \
  --collect-data librosa --collect-submodules librosa \
  --collect-data soundfile --collect-binaries soundfile \
  --copy-metadata librosa --copy-metadata soxr \
  run_gui.py
echo "→ dist/MultiLoop Slicer.app"
