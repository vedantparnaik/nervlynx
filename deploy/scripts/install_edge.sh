#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET_DIR="${1:-/opt/nervlynx}"

echo "[nervlynx] installing to ${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"
rsync -a --delete --exclude ".git" --exclude ".venv" "${ROOT_DIR}/" "${TARGET_DIR}/"

python3 -m venv "${TARGET_DIR}/.venv"
source "${TARGET_DIR}/.venv/bin/activate"
pip install -U pip
pip install -e "${TARGET_DIR}[dev]"

echo "[nervlynx] install complete"
