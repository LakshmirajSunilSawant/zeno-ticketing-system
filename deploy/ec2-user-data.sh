#!/bin/bash
# EC2 bootstrap script. Paste this into "Advanced details > User data" when launching the instance.
#
# WHY user data instead of SSH-ing in and typing commands: the instance is reproducible. If it dies
# or I need a second one, I relaunch from this script and get an identical box - no undocumented
# manual steps, no "what did I install again?". It's the cheapest form of infrastructure-as-code.
#
# Runs as root on first boot only. Output goes to /var/log/cloud-init-output.log.
set -euxo pipefail

REPO_URL="https://github.com/LakshmirajSunilSawant/zeno-ticketing-system.git"
APP_DIR="/opt/zeno"

# Amazon Linux 2023 uses dnf.
dnf update -y
dnf install -y docker git

systemctl enable --now docker
# Lets you run docker without sudo after reconnecting over SSH.
usermod -aG docker ec2-user

# Docker Compose v2 ships as a CLI plugin and is not in the AL2023 repos, so install it directly.
# ARCH detection means this same script works on x86_64 (t3.micro) and arm64 (t4g.micro).
ARCH="$(uname -m)"
COMPOSE_DIR=/usr/local/lib/docker/cli-plugins
mkdir -p "$COMPOSE_DIR"
curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-${ARCH}" \
  -o "$COMPOSE_DIR/docker-compose"
chmod +x "$COMPOSE_DIR/docker-compose"

git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

# WHY secrets are generated ON the instance: they never exist in git, in this script, or on my
# laptop. Read them back later with `sudo cat /opt/zeno/.env`.
# ADMIN_PASSWORD is left readable so you can log in for the demo; rotate it if this box outlives it.
cat > "$APP_DIR/.env" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
ADMIN_EMAIL=admin@zeno.dev
ADMIN_PASSWORD=$(openssl rand -hex 12)
EOF
chmod 600 "$APP_DIR/.env"
chown -R ec2-user:ec2-user "$APP_DIR"

# --build because we deploy from source; the image is built on the instance itself.
docker compose -f "$APP_DIR/docker-compose.yml" --project-directory "$APP_DIR" up -d --build

echo "=== Zeno is up. Credentials: ==="
cat "$APP_DIR/.env"
