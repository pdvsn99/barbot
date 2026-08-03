"""
hardware.py — pumps and scale.

Runs in one of two modes:

  MOCK  = True   no GPIO at all. Fake scale, fake pumps. Runs on your laptop.
  MOCK  = False  real Raspberry Pi with gpiozero installed.

The rest of the program only ever talks to `Pumps` and `Scale`, so nothing
else needs to change when you move from laptop to Pi.
"""

import time
import threading
import statistics
import collections

# ---------------------------------------------------------------------------
# Set this to False once you're running on the Pi with hardware attached.
#
# On the Pi, don't edit this file's settings directly — it lives in git and a
# future `git pull` would reset them. Instead, create a file called
# hardware_local.py next to this one with whichever of these you need to
# override, for example:
#
#     MOCK = False
#     ACTIVE_LOW = False
#
# That file is gitignored, so it survives updates. Restart test_app.py or
# barbot after changing it — Python only reads it when the process starts.
# ---------------------------------------------------------------------------
MOCK = True

# BCM pin numbers, one per pump line. Line 1 is the first in this list.
# These avoid the I2C, SPI and UART pins so you keep those free.
PUMP_PINS = [17, 27, 22, 5, 6, 13, 19, 26]

# HX711 load cell amplifier
HX711_DATA_PIN = 20
HX711_CLOCK_PIN = 21

# Most cheap relay boards are ACTIVE LOW: pulling the pin low turns the relay
# on. MOSFET boards are usually active high. If your pumps run when they should
# be idle, flip this. If they never run at all, that's the same mismatch the
# other way round — flip it either way.
ACTIVE_LOW = True

try:
    from hardware_local import *  # noqa: F401,F403
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Pumps
# ---------------------------------------------------------------------------

class Pumps:
    """One output per peristaltic pump. Speed only works on a MOSFET board."""

    def __init__(self, pins=PUMP_PINS):
        self.pins = pins
        self.count = len(pins)
        self._state = [0.0] * self.count
        self._devices = []

        if not MOCK:
            from gpiozero import PWMOutputDevice
            for pin in pins:
                self._devices.append(
                    PWMOutputDevice(pin, active_high=not ACTIVE_LOW, initial_value=0)
                )

    def set(self, line, speed):
        """line is 1-based. speed is 0.0 to 1.0."""
        speed = max(0.0, min(1.0, float(speed)))
        self._state[line - 1] = speed
        if not MOCK:
            self._devices[line - 1].value = speed

    def on(self, line):
        self.set(line, 1.0)

    def off(self, line):
        self.set(line, 0.0)

    def all_off(self):
        for line in range(1, self.count + 1):
            self.off(line)

    def speed_of(self, line):
        return self._state[line - 1]

    def any_running(self):
        return any(s > 0 for s in self._state)


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

class Scale:
    """
    HX711 load cell reader.

    A background thread reads the chip continuously and keeps the last handful
    of raw values. Everything else reads from that buffer instead of touching
    the chip directly. Two reasons:

      - The HX711 only produces 10 readings a second, so a direct read blocks
        for up to 100ms. With the pour loop and the browser both polling, that
        adds up fast on a slow Pi.
      - Bit-banging in Python occasionally produces a garbage value. Taking the
        median of the buffer throws those away.

    Call start_reader() once at startup.
    """

    def __init__(self, offset=0.0, scale_factor=1.0):
        self.offset = offset            # raw reading with nothing on the scale
        self.scale_factor = scale_factor  # raw units per gram
        self._lock = threading.Lock()
        self._recent = collections.deque(maxlen=20)
        self._reader_started = False
        self.last_error = None

        # Mock state
        self._mock_grams = 0.0
        self._mock_glass = 0.0

        if not MOCK:
            from gpiozero import DigitalOutputDevice, DigitalInputDevice
            self._clk = DigitalOutputDevice(HX711_CLOCK_PIN)
            self._dat = DigitalInputDevice(HX711_DATA_PIN)
            self._clk.off()

    def start_reader(self):
        """Begin sampling in the background. Safe to call more than once."""
        if self._reader_started:
            return
        self._reader_started = True

        def loop():
            while True:
                try:
                    value = self._read_raw_once()
                    self._recent.append(value)
                    self.last_error = None
                except Exception as e:
                    self.last_error = str(e)
                    time.sleep(0.2)
                time.sleep(0.005)

        threading.Thread(target=loop, daemon=True).start()

        # Give the buffer a moment to fill before anything asks for a weight.
        deadline = time.time() + 3.0
        while not self._recent and time.time() < deadline:
            time.sleep(0.05)

    # -- raw layer ----------------------------------------------------------

    def _read_raw_once(self):
        if MOCK:
            return self._mock_grams * 1000.0

        # Wait for the chip to signal "data ready" by pulling the line low.
        deadline = time.time() + 1.0
        while self._dat.value == 1:
            if time.time() > deadline:
                raise RuntimeError("Load cell not responding. Check wiring.")
            time.sleep(0.001)

        value = 0
        for _ in range(24):
            self._clk.on()
            self._clk.off()
            value = (value << 1) | self._dat.value

        # One extra pulse selects channel A at gain 128 for the next reading.
        self._clk.on()
        self._clk.off()

        # 24-bit two's complement
        if value & 0x800000:
            value -= 0x1000000
        return float(value)

    def read_raw(self, samples=5):
        """Median of the most recent `samples` background readings."""
        if not self._reader_started:
            self.start_reader()
        deadline = time.time() + 3.0
        while not self._recent and time.time() < deadline:
            time.sleep(0.05)
        values = list(self._recent)[-max(1, samples):]
        if not values:
            raise RuntimeError(
                self.last_error or "Load cell not responding. Check wiring.")
        return statistics.median(values)

    # -- grams --------------------------------------------------------------

    def grams(self, samples=3):
        """Absolute weight on the platform, in grams."""
        if self.scale_factor == 0:
            return 0.0
        return (self.read_raw(samples) - self.offset) / self.scale_factor

    def tare(self):
        """Call with the platform empty. Sets the zero point."""
        self.offset = self.read_raw(samples=15)
        return self.offset

    def calibrate(self, known_grams):
        """Call with a known weight on the platform, after taring."""
        raw = self.read_raw(samples=15)
        delta = raw - self.offset
        if known_grams <= 0 or delta == 0:
            raise ValueError("Calibration needs a real weight on the scale.")
        self.scale_factor = delta / known_grams
        return self.scale_factor

    # -- mock helpers -------------------------------------------------------

    def mock_add(self, grams):
        self._mock_grams += grams

    def mock_set_glass(self, grams):
        self._mock_grams = grams
        self._mock_glass = grams


# ---------------------------------------------------------------------------
# Mock pour simulation
# ---------------------------------------------------------------------------

def start_mock_flow(pumps, scale, ml_per_min=100.0):
    """
    In mock mode, pretend liquid arrives on the scale whenever a pump is on.
    Adds a little noise and a little lag so the abort logic gets exercised.
    """
    if not MOCK:
        return

    def loop():
        tick = 0.05
        while True:
            for line in range(1, pumps.count + 1):
                speed = pumps.speed_of(line)
                if speed > 0:
                    grams_per_tick = (ml_per_min / 60.0) * tick * speed
                    scale.mock_add(grams_per_tick)
            time.sleep(tick)

    threading.Thread(target=loop, daemon=True).start()
