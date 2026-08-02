# Bar Bot

## Getting to it

Once `bootstrap.sh` has run, the Pi answers on port 80 — no `:5000` to
remember:

    http://barbot.local

That name comes from mDNS. The Pi is renamed to `barbot` and `avahi-daemon`
publishes it, so iPhones, Macs, Windows and Android 12+ all find it with
nothing to install or configure. Its IP address still works too, if you're on
a network that blocks mDNS (some guest and corporate wifi does).

### Using bar.bot instead

`.local` is the only suffix mDNS is allowed to answer for, and `.bot` is a
real public top-level domain, so nothing on the Pi can claim that name by
itself — something on the network has to be told about it. Your router is the
right place:

1. **Reserve the Pi's address.** Find the DHCP reservation / static lease page
   and pin the Pi's current IP to its MAC address. `bootstrap.sh` prints both
   when it finishes. Without this the Pi can get a different address after a
   power cut and the name stops working.
2. **Add a local DNS entry** pointing `bar.bot` at that address. Routers call
   this different things — "static DNS", "local DNS", "DNS host names", "host
   entries". OpenWrt, UniFi, pfSense, Fritz!Box and most ISP routers have it
   somewhere; a few cheap ones don't, in which case `barbot.local` is your
   answer.

The app itself needs no changes — it answers to any name that reaches it.
`barbot.local` carries on working alongside.

## Running it off the Pi

    PORT=5000 python3 app.py

Port 80 needs root, which is worth avoiding on your laptop. On the Pi, systemd
grants the app `CAP_NET_BIND_SERVICE` so it can bind port 80 while still
running as an ordinary user.

Without a Pi it starts in mock mode: fake pumps, fake scale, drinks that pour
on screen and nowhere else. The Care tab has a switch for it.

## Mock mode

The switch lives in the Care tab, and there's a `MOCK` chip in the header
whenever it's on. The choice is saved in `settings.json`, which is untracked —
`update.sh` resets tracked files to match GitHub every couple of minutes, so
anything remembered in a tracked file wouldn't survive.

Turning it off drives the real pumps and the real load cell. With nothing
wired up that's electrically harmless — the GPIO pins have nothing to drive —
but a disconnected HX711 reads a steady zero rather than reporting a fault, so
the app can't tell "no scale attached" from "nothing on the tray".
