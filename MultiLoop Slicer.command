#!/bin/bash
# Lanceur macOS : double-cliquez sur ce fichier.
# Au premier lancement, il crée un environnement Python local (.venv)
# et installe les dépendances (quelques minutes, connexion internet requise).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Première installation…"
  PY=$(command -v python3) || { echo "Python 3 introuvable : installez-le depuis python.org"; read -r; exit 1; }
  "$PY" -m venv .venv && .venv/bin/pip install --upgrade pip -q && .venv/bin/pip install -r requirements.txt \
    || { echo "Échec de l'installation."; read -r; exit 1; }
fi
exec .venv/bin/python -m loopslicer
