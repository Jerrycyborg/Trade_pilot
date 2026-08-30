#!/bin/bash
set -euo pipefail

ROOT_DIR="/home/jarvis/Documents/Personal Projects/Trade_pilot"
STATE_DIR="$HOME/.trade-pilot"
TLS_DIR="$STATE_DIR/tls"
NGINX_CONF="$STATE_DIR/nginx.conf"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_DIR/tp-nginx.service"
LAN_IP_SCRIPT="$ROOT_DIR/scripts/get-lan-ip.sh"
NGINX_BIN="$(which nginx 2>/dev/null || true)"

# Keys are injected into the generated config so nginx supplies them on the
# server side. The browser must never hold ADMIN_API_KEY: anyone with it can
# toggle the kill switch and live mode.
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a; source "$ROOT_DIR/.env"; set +a
fi
: "${INTERNAL_API_KEY:=}"
: "${ADMIN_API_KEY:=}"
if [[ -z "$INTERNAL_API_KEY" ]]; then
  echo "warning: INTERNAL_API_KEY is unset — proxied API calls will 401." >&2
fi

if [[ -z "$NGINX_BIN" ]]; then
  echo "nginx is not installed."
  echo "Install it first, then rerun this script."
  echo "Ubuntu/Debian: sudo apt-get update && sudo apt-get install -y nginx"
  echo "Fedora: sudo dnf install -y nginx"
  echo "Arch: sudo pacman -S nginx"
  exit 1
fi

mkdir -p "$TLS_DIR" "$SYSTEMD_DIR" "$STATE_DIR/logs" "$STATE_DIR/client_temp" "$STATE_DIR/proxy_temp" \
  "$STATE_DIR/fastcgi_temp" "$STATE_DIR/uwsgi_temp" "$STATE_DIR/scgi_temp"

if [[ ! -f "$TLS_DIR/cert.pem" || ! -f "$TLS_DIR/key.pem" ]]; then
  openssl req -x509 -newkey rsa:4096 -keyout "$TLS_DIR/key.pem" \
    -out "$TLS_DIR/cert.pem" -days 3650 -nodes \
    -subj "/CN=trade-pilot/O=TradePilot"
fi

cat > "$NGINX_CONF" <<EOF
worker_processes 1;
pid $STATE_DIR/nginx.pid;
error_log $STATE_DIR/logs/error.log;

events {
  worker_connections 1024;
}

http {
  include       /etc/nginx/mime.types;
  default_type  application/octet-stream;
  sendfile      on;

  access_log $STATE_DIR/logs/access.log;
  client_body_temp_path $STATE_DIR/client_temp;
  proxy_temp_path $STATE_DIR/proxy_temp;
  fastcgi_temp_path $STATE_DIR/fastcgi_temp;
  uwsgi_temp_path $STATE_DIR/uwsgi_temp;
  scgi_temp_path $STATE_DIR/scgi_temp;

  server {
    listen 8443 ssl;
    server_name _;

    ssl_certificate $TLS_DIR/cert.pem;
    ssl_certificate_key $TLS_DIR/key.pem;

    root $ROOT_DIR/apps/dashboard;
    index index.html;

    add_header Access-Control-Allow-Origin * always;
    add_header Access-Control-Allow-Headers "Content-Type, X-Internal-Key, X-Admin-Key" always;
    add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;

    if (\$request_method = OPTIONS) {
      return 204;
    }

    location / {
      try_files \$uri \$uri/ /index.html;
    }

    location /api/policy/ {
      proxy_pass http://localhost:8001/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/execution/ {
      proxy_pass http://localhost:8002/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/strategy/ {
      proxy_pass http://localhost:8003/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/portfolio/ {
      proxy_pass http://localhost:8004/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/research/ {
      proxy_pass http://localhost:8005/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/audit/ {
      proxy_pass http://localhost:8006/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/orchestrator/ {
      proxy_pass http://localhost:8007/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/sentiment/ {
      proxy_pass http://localhost:8008/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/notification/ {
      proxy_pass http://localhost:8009/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }

    location /api/approval/ {
      proxy_pass http://localhost:8010/;
      proxy_set_header Host \$host;
      proxy_set_header X-Internal-Key "$INTERNAL_API_KEY";
      proxy_set_header X-Admin-Key "$ADMIN_API_KEY";
    }
  }
}
EOF

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Trade Pilot Nginx Reverse Proxy
After=network.target

[Service]
Type=simple
ExecStart=$NGINX_BIN -c "$NGINX_CONF" -g "daemon off;"
ExecReload=$NGINX_BIN -s reload -c "$NGINX_CONF"
ExecStop=$NGINX_BIN -s stop -c "$NGINX_CONF"
Restart=on-failure

[Install]
WantedBy=default.target
EOF

LAN_IP="unknown"
if [[ -x "$LAN_IP_SCRIPT" ]]; then
  LAN_IP="$("$LAN_IP_SCRIPT")"
fi

echo "Nginx config written to: $NGINX_CONF"
echo "Systemd user service written to: $SERVICE_FILE"
echo "TLS cert written to: $TLS_DIR/cert.pem"
echo "TLS key written to: $TLS_DIR/key.pem"
echo
echo "Next steps:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now tp-nginx.service"
echo "  systemctl --user status tp-nginx.service"
echo
echo "LAN IP: $LAN_IP"
echo "HTTPS URL: https://$LAN_IP:8443"
echo "The certificate is self-signed, so browsers will show a warning until you trust it."
