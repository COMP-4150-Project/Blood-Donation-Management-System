#!/usr/bin/env bash
# Run ON the Oracle VM (Ubuntu) as root:  sudo bash enable-https.sh [domain]
# Caddy (auto Let's Encrypt) in front of the running Vytal container. The app is only moved to 127.0.0.1:7860 AFTER Caddy is installed, so a failed download never takes the site down.
set -euo pipefail
DOMAIN="${1:-vytal.auctoraworld.com}"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

# 0. diagnostics + make sure sshd is up
echo "ssh service: $(systemctl is-active ssh 2>/dev/null || systemctl is-active sshd)"; systemctl enable --now ssh >/dev/null 2>&1 || true
echo "--- INPUT rules:"; iptables -S INPUT | sed -n 1,12p; echo "--- OUTPUT policy/rules:"; iptables -S OUTPUT | sed -n 1,5p

# 1. firewall: allow inbound 22/80/443 and all outbound (Oracle images can REJECT these); persist
for p in 22 80 443; do iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT; done
iptables -C OUTPUT -j ACCEPT 2>/dev/null || iptables -I OUTPUT 1 -j ACCEPT
command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
curl -sS -m 15 -o /dev/null https://dl.cloudsmith.io/ && echo "outbound https: ok" || { echo "outbound https STILL blocked, aborting before touching the app"; exit 1; }

# 2. install Caddy (it may fail to bind :80 while the app still holds it; that is fine)
if ! command -v caddy >/dev/null; then
  apt-get update -y >/dev/null
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg >/dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y >/dev/null
  apt-get install -y caddy >/dev/null || true
fi
command -v caddy >/dev/null || { echo "caddy install failed, app untouched"; exit 1; }
printf '%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:7860\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile

# 3. only now move the app off port 80 and start Caddy
CID=$(docker ps --filter publish=80 --format '{{.ID}}' | head -1)
[ -n "$CID" ] || CID=$(docker ps -q --filter name=vytal | head -1)
[ -n "$CID" ] || { echo "no running Vytal container found"; exit 1; }
IMG=$(docker inspect "$CID" --format '{{.Config.Image}}'); NAME=$(docker inspect "$CID" --format '{{.Name}}' | sed 's|^/||')
echo "container $NAME image $IMG"
docker rm -f "$CID" >/dev/null
docker run -d --name "$NAME" --restart always -p 127.0.0.1:7860:7860 "$IMG" >/dev/null
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 10
echo "caddy: $(systemctl is-active caddy)"
