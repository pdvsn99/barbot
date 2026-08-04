"""
hx711fast.py — the same read, done fast enough that the chip keeps up.

gpiozero takes tens of microseconds per pin write. The HX711 switches itself
off if the clock line stays high for more than 60 of them, so most reads were
being cut short by the chip powering down halfway through. This talks to
lgpio directly instead, which is a microsecond or two per write.

Run it and don't touch anything:

    python3 hx711fast.py

It reports how many readings survive, and how much they wander.
"""

import sys
import gc
import time
import statistics

DATA_PIN = 20
CLOCK_PIN = 21
SAMPLES = 100

try:
    import lgpio
except ImportError:
    sys.exit("lgpio isn't installed.  sudo apt install python3-lgpio")


def open_chip():
    """Pi 5 numbers its gpiochip differently to earlier models."""
    last = None
    for n in (0, 4):
        try:
            return lgpio.gpiochip_open(n)
        except Exception as e:
            last = e
    sys.exit("Couldn't open a gpiochip: %s" % last)


chip = open_chip()
lgpio.gpio_claim_output(chip, CLOCK_PIN, 0)
lgpio.gpio_claim_input(chip, DATA_PIN)

write = lgpio.gpio_write      # bound locally — attribute lookup costs time
read = lgpio.gpio_read


def reset():
    """Hold the clock high long enough to power the chip down, then wake it.

    This is the documented way back to a known state after a dropped read.
    """
    write(chip, CLOCK_PIN, 1)
    time.sleep(0.0001)
    write(chip, CLOCK_PIN, 0)
    time.sleep(0.0001)


def ready(timeout=1.0):
    deadline = time.time() + timeout
    while read(chip, DATA_PIN) == 1:
        if time.time() > deadline:
            return False
        time.sleep(0.001)
    return True


def read_raw():
    """One 24-bit reading, or None if the chip wasn't ready."""
    if not ready():
        return None

    # The 24-bit loop must not be interrupted. A collection pause here is
    # long enough for the chip to power itself down mid-read.
    gc.disable()
    try:
        value = 0
        for _ in range(24):
            write(chip, CLOCK_PIN, 1)
            write(chip, CLOCK_PIN, 0)
            value = (value << 1) | read(chip, DATA_PIN)
        write(chip, CLOCK_PIN, 1)     # 25th pulse: channel A, gain 128
        write(chip, CLOCK_PIN, 0)
    finally:
        gc.enable()

    if value & 0x800000:
        value -= 0x1000000
    return value


def read_valid(tries=5):
    """A reading that isn't obviously corrupt."""
    for _ in range(tries):
        v = read_raw()
        if v is not None and v not in (-1, 0) and abs(v) < 0x7FFFFF:
            return v
        reset()
    return None


print("Taking %d readings. Don't touch anything." % SAMPLES)
reset()

good, bad = [], 0
for i in range(SAMPLES):
    v = read_valid()
    if v is None:
        bad += 1
    else:
        good.append(v)
    if (i + 1) % 20 == 0:
        print("   %d..." % (i + 1))
    time.sleep(0.02)

lgpio.gpio_free(chip, CLOCK_PIN)
lgpio.gpio_free(chip, DATA_PIN)
lgpio.gpiochip_close(chip)

print("\n" + "-" * 52)
print("Good readings: %d" % len(good))
print("Gave up on:    %d" % bad)

if not good:
    print("\nStill nothing usable. That points away from timing now.")
    print("Check the HX711 is on 3.3V, and that the load cell's four wires")
    print("are in E+ E- A+ A- rather than the Pi-side pads.")
    sys.exit()

spread = max(good) - min(good)
print("Range at rest: %s to %s" % (format(min(good), ",d"), format(max(good), ",d")))
print("Spread:        %s counts" % format(spread, ",d"))
if len(good) > 2:
    print("Std deviation: %s counts" % format(int(statistics.stdev(good)), ",d"))

print()
if spread < 2000:
    print("That's a working scale. Steady enough to weigh a drink.")
    print("Say the word and I'll fold this reader into bench.py.")
elif spread < 20000:
    print("Much better, but still noisier than it should be.")
    print("Usually means the load cell wiring or a loose screw terminal.")
else:
    print("Still wandering badly. The chip is being read correctly now, so")
    print("the remaining problem is on the load cell side of the board.")
