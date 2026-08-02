#!/bin/bash
# Sets up a fresh Raspberry Pi from nothing. Safe to run more than once.
#
# Either called automatically on first boot (see README), or by hand:
#   curl -sSL https://raw.githubusercontent.com/YOURNAME/barbot/main/bootstrap.sh | sudo bash

set -e

REPO_URL="https://github.com/pdvsn99/barbot.git"
BRANCH="main"

# Whoever the Imager set up — won't necessarily be called "pi".
USERNAME=$(id -nu 1000 2>/dev/null || echo pi)
HOME_DIR=$(getent passwd "$USERNAME" | cut -d: -f6)
APP_DIR="$HOME_DIR/barbot"

echo "Setting up for user $USERNAME in $APP_DIR"

# Raspberry Pi OS Bookworm refuses plain `pip install` (PEP 668), so take
# everything from apt where possible.
apt-get update -qq
apt-get install -y -qq git python3-flask python3-gpiozero python3-lgpio curl

if [ -d "$APP_DIR/.git" ]; then
    echo "Already cloned — fetching latest"
    sudo -u "$USERNAME" git -C "$APP_DIR" fetch origin "$BRANCH" --quiet
    sudo -u "$USERNAME" git -C "$APP_DIR" reset --hard "origin/$BRANCH" --quiet
else
    sudo -u "$USERNAME" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

chmod +x "$APP_DIR/update.sh" 2>/dev/null || true

# --- the app itself -------------------------------------------------------

cat > /etc/systemd/system/barbot.service <<EOF
[Unit]
Description=Cocktail machine
After=network.target

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/app.py
WorkingDirectory=$APP_DIR
Restart=always
User=$USERNAME

[Install]
WantedBy=multi-user.target
EOF

# --- pulling updates from GitHub -----------------------------------------

cat > /etc/systemd/system/barbot-update.service <<EOF
[Unit]
Description=Pull cocktail machine updates from GitHub
After=network-online.target

[Service]
Type=oneshot
ExecStart=$APP_DIR/update.sh
User=$USERNAME
EOF

cat > /etc/systemd/system/barbot-update.timer <<'EOF'
[Unit]
Description=Check GitHub every two minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

# update.sh restarts the app, which needs root — allow just that one command.
echo "$USERNAME ALL=(ALL) NOPASSWD: /bin/systemctl restart barbot" \
    > /etc/sudoers.d/barbot
chmod 440 /etc/sudoers.d/barbot

# update.sh has the paths hardcoded for the default user; fix them if needed.
sed -i "s|^REPO=.*|REPO=\"$APP_DIR\"|" "$APP_DIR/update.sh"

systemctl daemon-reload
systemctl enable --now barbot.service
systemctl enable --now barbot-update.timer

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "Done. Open http://$IP:5000"
