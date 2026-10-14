#!/bin/sh
# Start the demo app for a phone.
#   ./run.sh            iPhone on the same Wi-Fi (or this Mac on the iPhone's hotspot)
#   ./run.sh --tunnel   iPhone anywhere, through a temporary public HTTPS link (needs: brew install cloudflared)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
cd "$HERE"

# Own virtual environment: nothing is installed into the main project.
if [ ! -x .venv/bin/python ]; then
    echo "Setting up the demo (first time only)…"
    python3 -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -r "$PROJECT/requirements.txt" -r requirements.txt
[ -f .env ] || cp .env.example .env

# Settings from .env (only the ones this script needs)
get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' ; }
PORT="${DEMO_PORT:-$(get DEMO_PORT)}"; PORT="${PORT:-8443}"
PIN="${DEMO_PIN:-$(get DEMO_PIN)}"
qr() { .venv/bin/python -c "import qrcode,sys; q=qrcode.QRCode(border=1); q.add_data(sys.argv[1]); q.print_ascii(invert=True)" "$1"; }

if [ "$1" = "--tunnel" ]; then
    command -v cloudflared >/dev/null || { echo "Install the tunnel first:  brew install cloudflared"; exit 1; }
    if [ -z "$PIN" ]; then PIN="$(od -An -N3 -tu4 /dev/urandom | tr -dc 0-9 | cut -c1-6)"; fi
    export DEMO_PIN="$PIN"
    .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000 --log-level warning &
    SERVER=$!
    trap 'kill $SERVER 2>/dev/null' EXIT INT TERM
    LOG="$(mktemp)"
    cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 >"$LOG" 2>&1 &
    TUNNEL=$!
    trap 'kill $SERVER $TUNNEL 2>/dev/null' EXIT INT TERM
    URL=""
    for i in $(seq 1 40); do URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)"; [ -n "$URL" ] && break; sleep 1; done
    [ -n "$URL" ] || { echo "The tunnel did not start:"; cat "$LOG"; exit 1; }
    echo; qr "$URL"
    echo "Open on the iPhone (scan the QR code with the Camera):  $URL"
    echo "PIN: $PIN"
    echo "Anyone with the link still needs the PIN. Stop with Ctrl-C (the link stops working)."
    wait $TUNNEL
    exit 0
fi

# Same network: HTTPS with a certificate for this Mac's address (Safari needs HTTPS for the microphone).
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
mkdir -p certs
if [ ! -f certs/cert.pem ] || [ "$(cat certs/ip.txt 2>/dev/null)" != "$IP" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 60 -keyout certs/key.pem -out certs/cert.pem \
        -subj "/CN=Rx Explainer demo" -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1
    echo "$IP" > certs/ip.txt
fi
URL="https://$IP:$PORT"
echo; qr "$URL"
echo "Open on the iPhone (same Wi-Fi; scan the QR code with the Camera):  $URL"
echo "Safari will say the connection is not private: tap Show Details > visit this website > Visit Website (once)."
echo "Then Share > Add to Home Screen to open it like an app."
[ -n "$PIN" ] && echo "PIN: $PIN" || echo "No PIN set (add DEMO_PIN in demo-app/.env to protect it)."
exec .venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port "$PORT" \
    --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem --log-level warning
