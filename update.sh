#!/bin/bash
# Checks GitHub for new commits and applies them.
#
# Run by a systemd timer every couple of minutes. Does nothing at all unless
# something actually changed, and refuses to restart while a drink is pouring.

set -u
# Work out where we are rather than hardcoding a path. This has to be
# self-locating: update.sh is tracked in git, so anything patched into it at
# install time would be wiped by the next reset.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

# If we already tried this commit and it wouldn't start, don't loop on it
# forever. Push a fix and we'll pick that up instead.
BADFILE="$REPO/.bad-commit"
if [ -f "$BADFILE" ] && [ "$(cat "$BADFILE")" = "$REMOTE" ]; then
    echo "Skipping $REMOTE — it failed to start last time"
    exit 0
fi

# Never yank the rug mid-pour. We'll catch it on the next run.
# If the app is down entirely curl fails, which is fine — we want to update.
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
    pip3 install -r requirements.txt --quiet --break-system-packages 2>/dev/null || true
fi

sudo systemctl restart "$SERVICE"

# Did it actually come back? Give it half a minute.
for _ in $(seq 1 15); do
    sleep 2
    if curl -s --max-time 2 "$URL/api/state" > /dev/null 2>&1; then
        echo "Restarted $SERVICE — healthy"
        rm -f "$BADFILE"
        exit 0
    fi
done

echo "App didn't come back. Rolling back to $(git rev-parse --short $LOCAL)"
echo "$REMOTE" > "$BADFILE"
git reset --hard "$LOCAL" --quiet
sudo systemctl restart "$SERVICE"
echo "Rolled back. Push a fix and it'll be picked up automatically."
