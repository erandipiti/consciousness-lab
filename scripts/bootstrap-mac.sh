#!/usr/bin/env bash
# Prepare a Mac to be the recording host (docs/DECISIONS.md D41).
#
# Idempotent: safe to re-run. It installs nothing without saying so, changes no
# system setting, and ends by running `probe env` so the first thing that
# happens on this machine is EVIDENCE about it rather than an assumption.
#
# It cannot grant Bluetooth permission — only a human clicking in System
# Settings can — so it checks what it can and tells you exactly what it cannot.
set -uo pipefail

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mcheck\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

REPO_URL="https://github.com/erandipiti/consciousness-lab.git"
failures=0

say "1. This machine"
if [ "$(uname -s)" != "Darwin" ]; then
  bad "this script is for macOS; on Linux the host is already set up"
  exit 1
fi
ok "macOS $(sw_vers -productVersion) on $(uname -m)"

say "2. Command line tools"
if xcode-select -p >/dev/null 2>&1; then
  ok "Xcode command line tools present"
else
  warn "installing Xcode command line tools — accept the dialog, then re-run this"
  xcode-select --install || true
  exit 1
fi

say "3. uv"
# uv provisions the pinned interpreter itself, so no system Python is needed.
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  warn "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || { bad "uv install failed"; exit 1; }
  # shellcheck disable=SC1091
  [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
  command -v uv >/dev/null 2>&1 || { bad "uv installed but not on PATH; open a new shell"; exit 1; }
  ok "uv $(uv --version | awk '{print $2}')"
fi

say "4. Repository"
if [ -f "pyproject.toml" ] && [ -d ".git" ]; then
  ok "already inside the checkout: $(pwd)"
else
  if [ ! -d "consciousness-lab" ]; then
    warn "cloning into $(pwd)/consciousness-lab"
    git clone "$REPO_URL" consciousness-lab || { bad "clone failed"; exit 1; }
  fi
  cd consciousness-lab || exit 1
  ok "checkout at $(pwd)"
fi

say "5. Locked environment"
# --locked refuses to proceed if the lock has drifted from pyproject.toml. That
# refusal is the point: a run against an unlocked environment proves nothing
# about reproducibility (docs/OPERATIONS.md).
if uv sync --locked --all-groups; then
  ok "environment matches uv.lock exactly"
else
  bad "uv sync --locked failed — do NOT work around it by dropping --locked"
  failures=$((failures + 1))
fi

say "6. The device libraries actually import"
uv run python - <<'PY'
import sys
for module in ("brainflow", "bleak", "polar_python", "serial", "pyarrow"):
    try:
        __import__(module)
        print(f"  ok    {module}")
    except Exception as exc:
        print(f"  FAIL  {module}: {type(exc).__name__}: {exc}")
        sys.exit(1)
PY
[ $? -eq 0 ] || failures=$((failures + 1))

say "7. Serial ports (the QT Py shows up here)"
# macOS names them /dev/cu.usbmodem*, not /dev/ttyACM* as on Linux.
if compgen -G "/dev/cu.usbmodem*" >/dev/null; then
  for port in /dev/cu.usbmodem*; do ok "$port"; done
else
  warn "no /dev/cu.usbmodem* right now — normal if the QT Py is unplugged"
fi

say "8. Bluetooth permission — the one thing this script cannot do"
cat <<'TXT'
  CoreBluetooth grants Bluetooth access to the APPLICATION, and a terminal does
  not have it by default. Without it every scan returns nothing and every device
  looks switched off.

    System Settings -> Privacy & Security -> Bluetooth -> enable your terminal
    then QUIT and reopen the terminal (not just a new tab)

  `probe scan` below is how you find out whether it worked. Run it before ever
  concluding a device is dead.
TXT

say "9. First evidence about this host"
uv run consciousness-lab probe env \
  --purpose "bootstrap: is this Mac ready to record" \
  --method "scripts/bootstrap-mac.sh on a fresh checkout" || failures=$((failures + 1))

say "Result"
if [ "$failures" -eq 0 ]; then
  ok "ready. Next: grant Bluetooth permission above, then follow docs/HARDWARE.md -> 'A bench session'"
else
  bad "$failures step(s) failed — fix those before running any device probe"
  exit 1
fi
