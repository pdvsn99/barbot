#!/bin/bash
# Checks GitHub for new commits and applies them.
#
# Run by a systemd timer every couple of minutes. Does nothing at all unless
# something actually changed, and refuses to restart while a drink is pouring.

set -u
REPO="/home/pi/barbot"
BRANCH="main"
SERVICE="barbot"
URL="http://localhost:5000"

cd "$REPO" || { echo "No repo at $REPO"; exit 1; }

git fetch origin "$BRANCH" --quiet || { echo "Can't reach GitHub"; exit 0; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

# Never yank the rug mid-pour. We'll catch it on the next run.
BUSY=$(curl -s --max-time 3 "$URL/api/state" | python3 -c \
    "import json,sys; print(json.load(sys.stdin)['status']['busy'])" 2>/dev/null)
if [ "$BUSY" = "True" ]; then
    echo "Busy pouring — leaving the update until next time"
    exit 0
fi

echo "Updating $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/$BRANCH)"

# reset rather than pull: the Pi should always match GitHub exactly, and this
# can't fail with a merge conflict at two in the morning.
git reset --hard "origin/$BRANCH" --quiet

if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "requirements.txt"; then
    echo "Dependencies changed — installing"
    pip3 install -r requirements.txt --quiet
fi

sudo systemctl restart "$SERVICE"
echo "Restarted $SERVICE"
