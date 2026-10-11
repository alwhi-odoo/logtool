#!/usr/bin/env bash
# Install logtool for the current user, so `logtool` and `logtool-report` run from any directory.
#
#   ./install.sh                       # from a clone or an unpacked source tree (this directory)
#   ./install.sh logtool-1.2.0.tar.gz  # or a release archive / wheel
#   ./install.sh git+https://github.com/alwhi-odoo/logtool.git   # or straight from GitHub
#   ./install.sh --uninstall           # remove it again
#
# It makes a private virtualenv (so nothing touches the system Python), installs logtool into it, and links its
# two commands into ~/.local/bin. If that directory is not on PATH yet, a line adding it goes into ~/.bashrc and
# ~/.zshrc (the ones that exist); open a new terminal afterwards. Running it again upgrades in place.
#
# Override with LOGTOOL_HOME (where the virtualenv goes), LOGTOOL_BIN (where the commands go) and PYTHON.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${LOGTOOL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/logtool}"
BIN="${LOGTOOL_BIN:-$HOME/.local/bin}"
PY="${PYTHON:-python3}"
CMDS=(logtool logtool-report)
MARK="# added by logtool install.sh"

rc_files() {
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ]; then echo "$rc"; fi
    done
    return 0
}

if [ "${1:-}" = "--uninstall" ]; then
    for c in "${CMDS[@]}"; do
        if [ -L "$BIN/$c" ] && [[ "$(readlink "$BIN/$c")" == "$HOME_DIR/"* ]]; then
            rm -f "$BIN/$c"
        fi
    done
    rm -rf "$HOME_DIR"
    for rc in $(rc_files); do                      # the PATH line this script added, and only that one
        if grep -qF "$MARK" "$rc"; then
            sed -i "/$MARK/,+1d" "$rc"
        fi
    done
    echo "logtool removed (uploaded logs live in ~/.cache/logtool while it runs; delete that too if you like)."
    exit 0
fi

TARGET="${1:-$SRC}"
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "logtool needs Python 3.9 or newer ($PY is $("$PY" -V 2>&1 || echo missing)). Set PYTHON=/path/to/python3." >&2
    exit 1
fi

echo "Installing logtool into $HOME_DIR"
if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
    if ! "$PY" -m venv "$HOME_DIR/venv"; then
        echo "Could not create a virtualenv. On Debian / Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    fi
fi
"$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade "$TARGET"

mkdir -p "$BIN"
for c in "${CMDS[@]}"; do
    ln -sfn "$HOME_DIR/venv/bin/$c" "$BIN/$c"
done

case ":$PATH:" in
    *":$BIN:"*) on_path=1 ;;
    *) on_path=0 ;;
esac
if [ "$on_path" = 0 ]; then
    added=0
    for rc in $(rc_files); do
        if ! grep -qF "$MARK" "$rc"; then
            printf '\n%s\ncase ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$MARK" "$BIN" "$BIN" >> "$rc"
            added=1
        fi
    done
    if [ "$added" = 1 ]; then
        echo "Added $BIN to PATH in $(rc_files | tr '\n' ' ')- open a new terminal (or: export PATH=\"$BIN:\$PATH\")."
    elif [ -z "$(rc_files)" ]; then
        echo "Add $BIN to your PATH to run logtool from anywhere."
    fi
fi

echo "Done: run 'logtool' from any directory (logtool --help, logtool-report --help)."
