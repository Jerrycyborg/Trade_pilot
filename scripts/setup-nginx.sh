#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/trade-pilot"
TLS_DIR="$STATE_DIR/tls"
NGINX_CONF="$STATE_DIR/nginx.conf"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SYSTEMD_DIR/tp-nginx.service"
NGINX_BIN="$(command -v nginx 2>/dev/null || true)"

if [[ -z "$NGINX_BIN" ]]; then
  echo "nginx is not installed."
  echo "Install nginx, then rerun this script."
  exit 1
fi

mkdir -p "$TLS_DIR" "$SYSTEMD_DIR" "$STATE_DIR/logs" "$STATE_DIR/client_temp" \
  "$STATE_DIR/proxy_temp" "$STATE_DIR/fastcgi_temp" "$STATE_DIR/uwsgi_temp" \
  "$STATE_DIR/scgi_temp"

if [[ ! -f "$TLS_DIR/cert.pem" || ! -f "$TLS_DIR/key.pem" ]]; then
  openssl req -x509 -newkey rsa:4096 -keyout "$TLS_DIR/key.pem" \
    -out "$TLS_DIR/cert.pem" -days 365 -nodes \
    -subj "/CN=localhost/O=TradePilot"
  chmod 600 "$TLS_DIR/key.pem"
fi

cat > "$NGINX_CONF" <<EOF
worker_processes 1;
pid $STATE_DIR/nginx.pid;
error_log $STATE_DIR/logs/error.log;

events {
  worker_connections 1024;
}

http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  sendfile on;
  server_tokens off;

  access_log $STATE_DIR/logs/access.log;
  client_body_temp_path $STATE_DIR/client_temp;
  proxy_temp_path $STATE_DIR/proxy_temp;
  fastcgi_temp_path $STATE_DIR/fastcgi_temp;
  uwsgi_temp_path $STATE_DIR/uwsgi_temp;
  scgi_temp_path $STATE_DIR/scgi_temp;

  server {
    listen 127.0.0.1:8443 ssl;
    server_name localhost;

    ssl_certificate $TLS_DIR/cert.pem;
    ssl_certificate_key $TLS_DIR/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    root $ROOT_DIR/apps/dashboard;
    index index.html;

    location / {
      limit_except GET { deny all; }
      try_files \$uri \$uri/ /index.html;
    }

    location /api/policy/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8001/;
    }
    location /api/execution/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8002/;
    }
    location /api/strategy/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8003/;
    }
    location /api/portfolio/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8004/;
    }
    location /api/research/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8005/;
    }
    location /api/audit/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8006/;
    }
    location /api/orchestrator/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8007/;
    }
    location /api/sentiment/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8008/;
    }
    location /api/notification/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8009/;
    }
    location /api/approval/ {
      limit_except GET { deny all; }
      proxy_pass http://127.0.0.1:8010/;
    }
  }
}
EOF

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Trade Pilot loopback-only dashboard proxy
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

echo "Loopback-only, read-only dashboard proxy configured."
echo "No API key is embedded in nginx or sent to a browser."
echo "Open https://localhost:8443 after starting tp-nginx.service."
echo "Use an authenticated operator CLI for mutations."
