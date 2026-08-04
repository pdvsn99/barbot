"""
hx711check.py — works out why the scale isn't reading.

Run it, then press the free end of the bar with a finger when it tells you to.

    python3 hx711check.py

It prints raw counts straight from the chip, with no zeroing or calibration in
the way, and then tells you which half of the setup is at fault.
"""

import sys
import time

DATA_PIN = 20
CLOCK_PIN = 21

try:
    from gpiozero import DigitalOutputDevice, DigitalInputDevice
except ImportError:
    sys.exit("gpiozero isn't installed.  sudo apt install python3-gpiozero")

clk = DigitalOutputDevice(CLOCK_PIN)
dat = DigitalInputDevice(DATA_PIN)
clk.off()


def read_raw(timeout=1.0):
    """One 24-bit reading. Returns None if the chip never says it's ready."""
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


def sample(n, label):
    print("\n%s" % label)
    readings = []
    for _ in range(n):
        v = read_raw()
        readings.append(v)
        print("   %s" % ("no response" if v is None else format(v, ">10,d")))
        time.sleep(0.1)
    return readings


print("Data pin  BCM %d   (physical pin 38)" % DATA_PIN)
print("Clock pin BCM %d   (physical pin 40)" % CLOCK_PIN)

print("\nResting level of the data line: %s"
      % ("high" if dat.value else "low"))

rest = sample(8, "Reading with nothing on the platform:")

input("\nNow press down on the free end of the bar and hold it. Press enter.")
load = sample(8, "Reading under load:")

clk.close()
dat.close()

# ---------------------------------------------------------------------------

print("\n" + "-" * 52)

if all(v is None for v in rest):
    print("The chip never signalled that a reading was ready.")
    print("The data line is stuck high, so nothing is driving it.")
    print("\n  - Check DT is on physical pin 38")
    print("  - Check VCC has 3.3V on it and GND is connected")
    sys.exit()

good_rest = [v for v in rest if v is not None]
good_load = [v for v in load if v is not None]

if all(v == 0 for v in good_rest):
    print("Every reading came back as exactly zero.")
    print("That is not a weight of zero — it is no data at all. The clock")
    print("pulses are going out and nothing is coming back.")
    print("\n  - Most likely: DT or SCK on the wrong header pin, or swapped")
    print("  - Or: the HX711 has no power. Check 3.3V across VCC and GND")
    sys.exit()

if all(v == -1 for v in good_rest):
    print("Every reading came back as -1, which is 24 bits all set high.")
    print("The data line is being held high the whole time.")
    print("\n  - Check the GND wire between the HX711 and the Pi")
    sys.exit()

spread = max(good_rest) - min(good_rest)
shift = (sum(good_load) / len(good_load)) - (sum(good_rest) / len(good_rest))

print("Resting counts:  %s to %s   (noise spread %s)"
      % (format(min(good_rest), ",d"), format(max(good_rest), ",d"),
         format(spread, ",d")))
print("Shift under load: %s counts" % format(int(shift), ",d"))

print()
if abs(shift) < spread * 3:
    print("The chip is talking, but pressing the bar barely changed anything.")
    print("So the HX711 side is fine and the load cell side is not.")
    print("\n  - The bar cannot flex. One end must be bolted down and the")
    print("    other free to move, with an air gap between them.")
    print("  - Or the four load cell wires are on the wrong pads. They go")
    print("    to E+ E- A+ A-, not to the pads on the Pi side of the board.")
elif shift < 0:
    print("Working, but counting backwards — pressing down lowers the value.")
    print("The bar is mounted the wrong way round for its arrow.")
    print("\n  - Swap which end is bolted to the base, or leave it and let")
    print("    calibrate() sort the sign out for you.")
else:
    print("This is all working. The chip reads, and load moves it the right way.")
    print("\n  - Run bench.py, set zero, then calibrate with a known weight.")
