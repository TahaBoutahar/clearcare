#!/bin/sh
# Install the prescription explainer as a single terminal command.
#
#   ./install.sh            installs the command "rx"
#   ./install.sh ordo       installs it under the name "ordo" instead
#   ./install.sh --remove [name]   removes it
#
# The command is a small launcher in ~/.local/bin that runs doctor_cli.py with this
# project's virtual environment, so it works from any folder.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"

if [ "$1" = "--remove" ]; then
    NAME="${2:-rx}"
    rm -f "$BIN_DIR/$NAME" && echo "Removed $BIN_DIR/$NAME"
    exit 0
fi

NAME="${1:-rx}"
case "$NAME" in
    *[!A-Za-z0-9_-]*|"") echo "Invalid command name: $NAME (use letters, digits, - or _)"; exit 1 ;;
esac

if [ -e "$BIN_DIR/$NAME" ] && ! grep -q "doctor_cli.py" "$BIN_DIR/$NAME" 2>/dev/null; then
    echo "$BIN_DIR/$NAME already exists and is not ours. Pick another name: ./install.sh <name>"
    exit 1
fi
if command -v "$NAME" >/dev/null 2>&1 && [ "$(command -v "$NAME")" != "$BIN_DIR/$NAME" ]; then
    echo "Note: '$NAME' already exists at $(command -v "$NAME"); yours may be shadowed. Consider another name."
fi

# Python environment
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "Creating virtual environment…"
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

# Settings file
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "Created settings file: $PROJECT_DIR/.env"
fi

# Launcher
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$NAME" <<LAUNCHER
#!/bin/sh
# Launcher for the prescription explainer ($PROJECT_DIR). Reinstall with install.sh.
RX_COMMAND_NAME="$NAME" exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/doctor_cli.py" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/$NAME"

echo "Installed: $NAME"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Add this to your ~/.zshrc so the command is found:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo "Try:  $NAME --help"
