#!/usr/bin/env bash
# Deploys the Vytal demo to a VPS you can SSH into (Hostinger VPS, Ubuntu/Debian):  ./deploy-vps.sh user@HOST
# Installs Docker if missing, copies this folder, builds the image and runs it on port 80 (restarts on reboot).
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
ssh "$HOST" "cd ~/vytal-demo && $SUDO docker build -t vytal-demo . && ($SUDO docker rm -f vytal 2>/dev/null || true) && $SUDO docker run -d --name vytal --restart always -p 80:7860 vytal-demo"
echo "Live (allow ~30 s to seed): http://${HOST#*@}/"
