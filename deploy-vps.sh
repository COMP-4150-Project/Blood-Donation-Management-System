#!/usr/bin/env bash
# Deploys the Vytal demo to a VPS you can SSH into (Hostinger VPS, Ubuntu/Debian):  ./deploy-vps.sh user@HOST
# Installs Docker if missing, copies this folder, builds the image and runs it (restarts on reboot).
# First run: the app is on port 80 over plain HTTP. After enable-https.sh, re-running this keeps the HTTPS setup:
# the container goes back on 127.0.0.1:7860 behind Caddy, with Secure session cookies and the sign-in throttle keyed on
# the client address Caddy forwards (VYTAL_TRUST_PROXY=1).
# Also installs a daily reseed: the container is restarted at 04:00 America/Toronto, which rebuilds the sample data.
set -euo pipefail
HOST="${1:?usage: ./deploy-vps.sh user@host}"
cd "$(dirname "$0")"
SUDO=$(ssh "$HOST" '[ "$(id -u)" = 0 ] && echo "" || echo sudo')
# small VMs (e.g. 1 GB Always Free Micro): add 2 GB swap so the image build and Postgres fit
ssh "$HOST" "[ \$(awk '/MemTotal/{print int(\$2/1024)}' /proc/meminfo) -ge 3000 ] || [ -f /swapfile ] || ($SUDO fallocate -l 2G /swapfile && $SUDO chmod 600 /swapfile && $SUDO mkswap /swapfile >/dev/null && $SUDO swapon /swapfile && echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null)"
# Oracle Ubuntu images ship an iptables REJECT rule that blocks port 80 even when the VCN allows it
ssh "$HOST" "$SUDO iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || $SUDO iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT; command -v netfilter-persistent >/dev/null && $SUDO netfilter-persistent save >/dev/null 2>&1 || true"
ssh "$HOST" "command -v docker >/dev/null || curl -fsSL https://get.docker.com | $SUDO sh"
tar --exclude=.git --exclude=.venv --exclude=__pycache__ -czf - . | ssh "$HOST" "rm -rf ~/vytal-demo && mkdir ~/vytal-demo && tar -xzf - -C ~/vytal-demo"

# HTTPS already set up (enable-https.sh: Caddy running and proxying to 127.0.0.1:7860)? Then stay behind it.
if ssh "$HOST" "systemctl is-active --quiet caddy 2>/dev/null && grep -q 'reverse_proxy 127.0.0.1:7860' /etc/caddy/Caddyfile 2>/dev/null"; then
  RUN_OPTS="-p 127.0.0.1:7860:7860 -e VYTAL_TRUST_PROXY=1"; URL="https://$(ssh "$HOST" "awk 'NR==1{print \$1}' /etc/caddy/Caddyfile")/"
else
  # plain HTTP: a Secure cookie would never come back, and with no proxy in front X-Forwarded-For is not trusted
  RUN_OPTS="-p 80:7860 -e VYTAL_SECURE_COOKIE=0 -e VYTAL_TRUST_PROXY=0"; URL="http://${HOST#*@}/"
fi
ssh "$HOST" "cd ~/vytal-demo && $SUDO docker build -t vytal-demo . && ($SUDO docker rm -f vytal 2>/dev/null || true) && $SUDO docker run -d --name vytal --restart always $RUN_OPTS vytal-demo"

# Daily reseed (L2): every start rebuilds the made-up data at today's date, so overdue requests and expired units never pile up.
# systemd timer at 04:00 America/Toronto (follows daylight saving); hosts without timezone-aware timers get an hourly cron check.
# Re-running overwrites the same files, so this is idempotent.
ssh "$HOST" "$SUDO sh -s" <<'REMOTE'
set -e
if command -v systemctl >/dev/null && [ -d /run/systemd/system ] && systemd-analyze calendar '*-*-* 04:00:00 America/Toronto' >/dev/null 2>&1; then
  cat > /etc/systemd/system/vytal-reseed.service <<'UNIT'
[Unit]
Description=Vytal demo daily reseed (restarting the container rebuilds the sample data)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'docker restart -t 20 vytal'
UNIT
  cat > /etc/systemd/system/vytal-reseed.timer <<'UNIT'
[Unit]
Description=Vytal demo reseed every day at 04:00 Toronto time

[Timer]
OnCalendar=*-*-* 04:00:00 America/Toronto
AccuracySec=1min
Persistent=false

[Install]
WantedBy=timers.target
UNIT
  rm -f /etc/cron.d/vytal-reseed
  systemctl daemon-reload
  systemctl enable --now vytal-reseed.timer >/dev/null 2>&1
  systemctl restart vytal-reseed.timer
  echo "reseed timer: $(systemctl show vytal-reseed.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo installed)"
else
  printf '%s\n' 'SHELL=/bin/sh' 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
    '# Vytal demo daily reseed at 04:00 America/Toronto (checked hourly, so daylight saving is handled)' \
    '0 * * * * root [ "$(TZ=America/Toronto date +\%H)" = 04 ] && docker restart -t 20 vytal >/dev/null 2>&1' > /etc/cron.d/vytal-reseed
  chmod 644 /etc/cron.d/vytal-reseed
  echo "reseed: cron entry /etc/cron.d/vytal-reseed"
fi
REMOTE
echo "Live (allow ~30 s to seed): $URL"
