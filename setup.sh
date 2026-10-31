#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
REVISION=9abe50ad2f02f6b51ec0b84257e2dd472cd6b3ab
if ! command -v brew >/dev/null; then
  echo 'Homebrew is required on macOS for libusb. See https://brew.sh' >&2
  exit 1
fi
if ! brew list libusb >/dev/null 2>&1; then
  brew install libusb
fi
if [[ ! -d vendor/PixCut-App/.git ]]; then
  mkdir -p vendor
  git clone https://github.com/popcornhax/PixCut-App.git vendor/PixCut-App
  git -C vendor/PixCut-App checkout --detach "$REVISION"
fi
if [[ "$(git -C vendor/PixCut-App rev-parse HEAD)" != "$REVISION" ]]; then
  echo "Unexpected driver revision. Expected $REVISION. Existing files were left intact." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  if command -v uv >/dev/null; then
    uv venv --python 3.12 .venv
  else
    if ! brew list python@3.12 >/dev/null 2>&1; then brew install python@3.12; fi
    "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
  fi
fi
if command -v uv >/dev/null; then
  uv pip sync --python .venv/bin/python requirements.lock
else
  .venv/bin/python -m pip install -r requirements.lock
fi
mkdir -p create jobs .state
chmod +x pixcut
echo 'Installed. Run ./pixcut doctor, then ./pixcut watch.'
