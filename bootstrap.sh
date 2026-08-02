#!/bin/bash
# Sets up a fresh Raspberry Pi from nothing. Safe to run more than once.
#
# Either called automatically on first boot (see README), or by hand:
#   curl -sSL https://raw.githubusercontent.com/pdvsn99/barbot/main/bootstrap.sh | sudo bash

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
apt-get install -y -qq git python3-flask python3-gpiozero python3-lgpio curl \
                      avahi-daemon

# --- give it a name -------------------------------------------------------
# The hostname is what avahi publishes, so calling the Pi "barbot" is what
# makes http://barbot.local work from any phone on the network with nothing
# to set up on the phone. .local is the only suffix mDNS can answer for; see
# the README for pointing a name like bar.bot at it instead.

WANTED_HOSTNAME="barbot"
RENAMED=0
if [ "$(hostname)" != "$WANTED_HOSTNAME" ]; then
    echo "Renaming $(hostname) -> $WANTED_HOSTNAME"
    # /etc/hosts first. Rename the machine without this and every later sudo
    # spends a few seconds failing to look itself up.
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$WANTED_HOSTNAME/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$WANTED_HOSTNAME" >> /etc/hosts
    fi
    hostnamectl set-hostname "$WANTED_HOSTNAME" 2>/dev/null \
        || echo "$WANTED_HOSTNAME" > /etc/hostname
    RENAMED=1
fi

systemctl enable --now avahi-daemon 2>/dev/null || true
if [ "$RENAMED" = "1" ]; then
    # An avahi that was already running carries on publishing the old name
    # until it's made to look again. --now above won't do it: the service is
    # started, so there's nothing for it to start.
    systemctl restart avahi-daemon 2>/dev/null || true
fi

if [ -d "$APP_DIR/.git" ]; then
    echo "Already cloned — fetching latest"
    sudo -u "$USERNAME" git -C "$APP_DIR" fetch origin "$BRANCH" --quiet
    sudo -u "$USERNAME" git -C "$APP_DIR" reset --hard "origin/$BRANCH" --quiet
    # update.sh writes this when a commit wouldn't start, and then refuses to
    # try it again. Running this script is the deliberate second attempt, and
    # it has just installed everything update.sh couldn't.
    rm -f "$APP_DIR/.bad-commit"
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
RestartSec=5
User=$USERNAME
# Serving on port 80 means no ":5000" to type. Ports below 1024 normally need
# root; this hands over that one power on its own instead, so the app still
# runs as $USERNAME.
AmbientCapabilities=CAP_NET_BIND_SERVICE

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

systemctl daemon-reload
systemctl enable barbot.service barbot-update.timer

# restart rather than `enable --now`: on a machine that already has these, the
# --now half is a no-op because the service is running, so a changed unit file
# — a new port, a new capability — would sit there doing nothing.
systemctl restart barbot.service
systemctl restart barbot-update.timer

# --- the machine's own screen (opt-in) ------------------------------------
# Only for the Pi with the square panel wired to it. It's off by default
# because the usual install is headless — a Zero 2 with no desktop — and this
# pulls in an X server and Chromium, which is a lot of card and a lot of RAM
# to spend on a machine with no screen to show it on.
#
#   curl -sSL .../bootstrap.sh | sudo KIOSK=1 bash
#
# Once set, it stays set: the flag is remembered below so later re-runs don't
# have to repeat it.

KIOSK_FLAG=/etc/barbot-kiosk
[ -f "$KIOSK_FLAG" ] && KIOSK=1

if [ "${KIOSK:-0}" = "1" ]; then
    echo "Setting up the panel (KIOSK=1)"
    touch "$KIOSK_FLAG"

    apt-get install -y -qq xserver-xorg xinit x11-xserver-utils unclutter
    # Named chromium on Bookworm, chromium-browser on older images. Ask for
    # both and take whichever landed.
    apt-get install -y -qq chromium || apt-get install -y -qq chromium-browser || true
    CHROMIUM=$(command -v chromium || command -v chromium-browser || true)

    if [ -z "$CHROMIUM" ]; then
        echo "No chromium available from apt — skipping the panel setup."
    else
        cat > /usr/local/bin/barbot-kiosk <<EOF
#!/bin/bash
# Full-screen browser on the panel, pointed at the square layout.
xset s off -dpms s noblank      # a bar that blanks mid-party is no use
unclutter -idle 0 &             # no mouse pointer parked over a button
exec $CHROMIUM \\
    --kiosk http://localhost/square \\
    --noerrdialogs --disable-infobars --incognito \\
    --check-for-update-interval=31536000 \\
    --disable-pinch --overscroll-history-navigation=0 \\
    --disable-session-crashed-bubble --disable-features=TranslateUI
EOF
        chmod +x /usr/local/bin/barbot-kiosk

        cat > /etc/systemd/system/barbot-kiosk.service <<EOF
[Unit]
Description=Cocktail machine panel
# The app serves the page, so there's nothing to show until it's up.
After=barbot.service
Wants=barbot.service

[Service]
ExecStart=/usr/bin/xinit /usr/local/bin/barbot-kiosk -- :0 vt1 -nocursor
Restart=always
RestartSec=5
User=$USERNAME
# xinit wants a console of its own to take over.
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal

[Install]
WantedBy=multi-user.target
EOF

        # Starting X needs permission that Debian withholds by default from
        # anyone not sitting at the machine — which, started by systemd, we
        # aren't.
        if [ -f /etc/X11/Xwrapper.config ]; then
            sed -i 's/^allowed_users=.*/allowed_users=anybody/' /etc/X11/Xwrapper.config
            grep -q '^needs_root_rights' /etc/X11/Xwrapper.config \
                || echo 'needs_root_rights=yes' >> /etc/X11/Xwrapper.config
        else
            printf 'allowed_users=anybody\nneeds_root_rights=yes\n' \
                > /etc/X11/Xwrapper.config
        fi
        usermod -aG tty,video "$USERNAME" || true

        systemctl daemon-reload
        systemctl enable barbot-kiosk.service
        systemctl restart barbot-kiosk.service
        KIOSK_MSG="The panel is showing http://localhost/square"
    fi
else
    KIOSK_MSG=""
fi

# --- Raspberry Pi Connect (browser-based remote shell) --------------------
# Shell-only variant: no desktop needed, works on a Zero 2. Signing in has to
# be done by hand once, since it needs you to open a link in a browser.
if apt-get install -y -qq rpi-connect-lite 2>/dev/null; then
    # Without lingering, Connect drops off whenever the user isn't logged in —
    # which on a headless machine is always.
    loginctl enable-linger "$USERNAME" || true
    sudo -u "$USERNAME" XDG_RUNTIME_DIR="/run/user/$(id -u "$USERNAME")" \
        systemctl --user enable --now rpi-connect 2>/dev/null || true
    CONNECT_MSG="Then run:  rpi-connect signin"
else
    CONNECT_MSG=""
fi

IP=$(hostname -I | awk '{print $1}')
IFACE=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')
MAC=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null)

echo ""
echo "Done. Open http://barbot.local"
echo "  ...or http://$IP if your network blocks mDNS."
echo ""
echo "Want it on bar.bot instead? That name can't come from mDNS, so your"
echo "router has to serve it. In the router's admin pages:"
echo "  1. Reserve $IP for MAC ${MAC:-the Pi} so the address stops moving."
echo "  2. Add a static DNS / local DNS entry:  bar.bot -> $IP"
echo "Nothing here depends on that — barbot.local keeps working either way."
# `|| true` because of `set -e` above: an empty message makes the test fail,
# which would take the whole script's exit status — and anything after it —
# down with it.
[ -n "$KIOSK_MSG" ] && echo "$KIOSK_MSG" || true
[ -n "$CONNECT_MSG" ] && echo "$CONNECT_MSG" || true
