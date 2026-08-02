#!/bin/bash
# Checks GitHub for new commits and applies them.
#
# Run by a systemd timer every couple of minutes. Does nothing at all unless
# something actually changed, and refuses to restart while a drink is pouring.

set -u

# Everything lives in main() because this script replaces itself half way
# through — `git reset --hard` rewrites update.sh while bash is still reading
# it. Bash reads a plain script incrementally by byte offset, so a version
# that changes length garbles the rest of the run. It parses a function whole
# before running any of it, so this makes the swap harmless.
main() {

# Work out where we are rather than hardcoding a path. This has to be
# self-locating: update.sh is tracked in git, so anything patched into it at
# install time would be wiped by the next reset.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="main"
SERVICE="barbot"

# 80 once bootstrap.sh has granted the capability, 5000 on a Pi set up before
# that or wherever the app has fallen back. Ask both rather than assume.
PORTS="80 5000"

# GET from whichever port answers. Body on stdout; non-zero if nothing did.
app_get() {
    local path="$1" port body
    for port in $PORTS; do
        body=$(curl -s --max-time 3 "http://localhost:$port$path" 2>/dev/null) \
            || continue
        if [ -n "$body" ]; then
            printf '%s' "$body"
            return 0
        fi
    done
    return 1
}

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
BUSY=$(app_get /api/state | python3 -c \
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
    if app_get /api/state > /dev/null 2>&1; then
        echo "Restarted $SERVICE — healthy"
        rm -f "$BADFILE"
        # bootstrap.sh owns the systemd unit, the hostname and the packages,
        # and nothing here can re-run it — it needs root. Say so when it has
        # fallen behind, rather than leaving you to wonder why a change to it
        # never showed up.
        if ! git diff --quiet "$LOCAL" "$REMOTE" -- bootstrap.sh; then
            echo "NOTE: bootstrap.sh changed in this update. Re-run it to pick"
            echo "      up the parts outside the app itself:"
            echo "      curl -sSL https://raw.githubusercontent.com/pdvsn99/barbot/main/bootstrap.sh | sudo bash"
        fi
        exit 0
    fi
done

echo "App didn't come back. Rolling back to $(git rev-parse --short $LOCAL)"
echo "$REMOTE" > "$BADFILE"
git reset --hard "$LOCAL" --quiet
sudo systemctl restart "$SERVICE"
echo "Rolled back. Push a fix and it'll be picked up automatically."

}

main "$@"
