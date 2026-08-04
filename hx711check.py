"""
hx711soak.py — how often do the readings go bad on their own?

Touch nothing while this runs. No weight, no hands, no nudging the wires.
It takes 100 readings and counts how many come back corrupted.

    python3 hx711soak.py
"""

import sys
import time
import statistics

DATA_PIN = 20
CLOCK_PIN = 21
SAMPLES = 100

try:
    from gpiozero import DigitalOutputDevice, DigitalInputDevice
except ImportError:
    sys.exit("gpiozero isn't installed.  sudo apt install python3-gpiozero")

clk = DigitalOutputDevice(CLOCK_PIN)
dat = DigitalInputDevice(DATA_PIN)
clk.off()


def read_raw(timeout=1.0):
    deadline = time.time() + timeout
    while dat.value == 1:
        if time.time() > deadline:
            return None
        time.sleep(0.001)

    value = 0
    for _ in range(24):
        clk.on()
        clk.off()
        value = (value << 1) | dat.value

    clk.on()
    clk.off()

    if value & 0x800000:
        value -= 0x1000000
    return value


print("Taking %d readings. Don't touch anything." % SAMPLES)

good, bad, timeouts = [], 0, 0
for i in range(SAMPLES):
    v = read_raw()
    if v is None:
        timeouts += 1
    elif v in (-1, 0):
        bad += 1
    else:
        good.append(v)
    if (i + 1) % 20 == 0:
        print("   %d..." % (i + 1))
    time.sleep(0.02)

clk.close()
dat.close()

print("\n" + "-" * 52)
print("Good readings:   %d" % len(good))
print("Corrupted:       %d" % bad)
print("Timed out:       %d" % timeouts)

if good:
    spread = max(good) - min(good)
    print("Range at rest:   %s to %s" % (format(min(good), ",d"), format(max(good), ",d")))
    print("Spread:          %s counts" % format(spread, ",d"))
    if len(good) > 2:
        print("Std deviation:   %s counts" % format(int(statistics.stdev(good)), ",d"))

rate = (bad + timeouts) / SAMPLES * 100
print("\nCorruption rate: %.0f%% with nothing touching the rig" % rate)

print()
if rate > 5:
    print("Bad readings happen on their own, so pressing the bar wasn't the")
    print("cause. This is the clock timing — Python can't hold the pulse")
    print("short enough, and the chip keeps dropping out.")
    print("\nFixable in software. Tell me this number and I'll rewrite the")
    print("reader to resync and retry.")
elif good and (max(good) - min(good)) > 5000:
    print("Readings are clean but very noisy for an untouched bar.")
    print("Check the load cell wires are firmly in the screw terminals, and")
    print("that the HX711 is on 3.3V rather than 5V.")
else:
    print("Rock steady while untouched. So the bad readings only appear when")
    print("you press the bar — something moves. Check the six wires, most")
    print("likely a dupont jumper that isn't fully seated.")
