"""
bench.py — a bench-test rig for the drinks machine.

One file, real hardware only. Run it on the Pi with the pumps and load cell
wired up:

    python3 bench.py

Then open http://<pi-address>:5001

If the real app is already running it will be holding the pins, so stop it
first:

    sudo systemctl stop barbot
"""

import sys
import gc
import time
import threading
import statistics
import collections

from flask import Flask, jsonify, request


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# BCM pin numbers, one per pump line. Line 1 is first in the list.
PUMP_PINS = [17, 27, 22, 5, 6, 13, 19, 26]

# HX711 load cell amplifier
HX711_DATA_PIN = 20
HX711_CLOCK_PIN = 21

# Which way round the driver board is. Most cheap relay boards want the pin
# pulled LOW to switch on; MOSFET boards want it HIGH. You can flip this from
# the interface while it's running, so the value here is only a starting guess
# — once you know which is right, set it here so it sticks.
ACTIVE_HIGH = True

# A pump can't run longer than this in one test, whatever the interface asks.
MAX_POUR_SECONDS = 30

PORT = 5001


# ---------------------------------------------------------------------------
# GPIO
# ---------------------------------------------------------------------------

# Pumps go through gpiozero, which gives us PWM for free and isn't timing
# critical. The load cell does not: gpiozero takes tens of microseconds per
# pin write, and the HX711 powers itself down if the clock line stays high
# for more than 60 of them, which corrupts the read. So the scale talks to
# lgpio directly. They use separate pins, so the two can coexist.
try:
    from gpiozero import PWMOutputDevice
    from gpiozero.exc import GPIOZeroError
except ImportError:
    sys.exit(
        "gpiozero isn't installed, so this can't reach the pumps.\n"
        "Install it with:  sudo apt install python3-gpiozero"
    )

try:
    import lgpio
except ImportError:
    sys.exit(
        "lgpio isn't installed, so this can't read the load cell.\n"
        "Install it with:  sudo apt install python3-lgpio"
    )


# ---------------------------------------------------------------------------
# Pumps
# ---------------------------------------------------------------------------

class Pumps:
    """One output per pump. Speed below 1.0 only does anything on a MOSFET board."""

    def __init__(self, pins=PUMP_PINS):
        self.pins = pins
        self.count = len(pins)
        self.active_high = ACTIVE_HIGH
        self._state = [0.0] * self.count
        self._devices = []

        for pin in pins:
            try:
                # Always built active-high. Polarity is applied in _write() so
                # it can be changed without rebuilding the devices.
                self._devices.append(
                    PWMOutputDevice(pin, active_high=True, initial_value=0))
            except GPIOZeroError as e:
                self.release()
                sys.exit(
                    "Couldn't take control of BCM pin %d: %s\n\n"
                    "Something else is already using it. If the machine is\n"
                    "running the real app, stop it first:\n"
                    "    sudo systemctl stop barbot" % (pin, e)
                )

        self._apply_all()

    def _write(self, index, speed):
        self._devices[index].value = speed if self.active_high else 1.0 - speed

    def _apply_all(self):
        for i, speed in enumerate(self._state):
            self._write(i, speed)

    def set_polarity(self, active_high):
        """Flip the driver board polarity and re-apply the current states."""
        self.active_high = bool(active_high)
        self._apply_all()

    def set(self, line, speed):
        """line is 1-based. speed is 0.0 to 1.0."""
        speed = max(0.0, min(1.0, float(speed)))
        self._state[line - 1] = speed
        self._write(line - 1, speed)

    def all_off(self):
        for line in range(1, self.count + 1):
            self.set(line, 0.0)

    def speed_of(self, line):
        return self._state[line - 1]

    def states(self):
        return list(self._state)

    def release(self):
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

class Scale:
    """
    HX711 load cell reader.

    A background thread reads the chip continuously into a small buffer, and
    everything else reads the median of that buffer. The chip only produces
    about ten readings a second, so reading it directly would block.

    Reads that come back corrupt are discarded and retried rather than
    averaged in — a dropped read returns all-ones, which would drag the
    median a long way if it were allowed through.
    """

    def __init__(self):
        self.offset = 0.0        # raw reading with nothing on the platform
        self.scale_factor = 1.0  # raw units per gram, set by calibrate()
        self.calibrated = False
        self.last_error = None
        self.discarded = 0       # corrupt reads thrown away since startup
        self._recent = collections.deque(maxlen=20)
        self._started = False

        self._chip = self._open_chip()
        lgpio.gpio_claim_output(self._chip, HX711_CLOCK_PIN, 0)
        lgpio.gpio_claim_input(self._chip, HX711_DATA_PIN)
        self._reset()

    @staticmethod
    def _open_chip():
        """Pi 5 numbers its gpiochip differently to earlier models."""
        last = None
        for n in (0, 4):
            try:
                return lgpio.gpiochip_open(n)
            except Exception as e:
                last = e
        sys.exit("Couldn't open a gpiochip for the load cell: %s" % last)

    def _reset(self):
        """Power the chip down and back up — the way back from a dropped read."""
        lgpio.gpio_write(self._chip, HX711_CLOCK_PIN, 1)
        time.sleep(0.0001)
        lgpio.gpio_write(self._chip, HX711_CLOCK_PIN, 0)
        time.sleep(0.0001)

    def start(self):
        if self._started:
            return
        self._started = True

        def loop():
            while True:
                try:
                    v = self._read_valid()
                    if v is None:
                        self.last_error = "No reply from the load cell. Check the wiring."
                        time.sleep(0.2)
                    else:
                        self._recent.append(v)
                        self.last_error = None
                except Exception as e:
                    self.last_error = str(e)
                    time.sleep(0.2)
                time.sleep(0.005)

        threading.Thread(target=loop, daemon=True).start()

        deadline = time.time() + 3.0
        while not self._recent and time.time() < deadline:
            time.sleep(0.05)

    def _read_raw_once(self):
        """One 24-bit reading, or None if the chip never signalled ready."""
        chip, clk, dat = self._chip, HX711_CLOCK_PIN, HX711_DATA_PIN
        write, read = lgpio.gpio_write, lgpio.gpio_read

        # The chip pulls the data line low when a reading is ready.
        deadline = time.time() + 1.0
        while read(chip, dat) == 1:
            if time.time() > deadline:
                return None
            time.sleep(0.001)

        # The 24-bit loop must not be interrupted. A collection pause here is
        # long enough for the chip to power itself down mid-read.
        gc.disable()
        try:
            value = 0
            for _ in range(24):
                write(chip, clk, 1)
                write(chip, clk, 0)
                value = (value << 1) | read(chip, dat)
            # One extra pulse picks channel A at gain 128 for the next reading.
            write(chip, clk, 1)
            write(chip, clk, 0)
        finally:
            gc.enable()

        if value & 0x800000:          # 24-bit two's complement
            value -= 0x1000000
        return float(value)

    def _read_valid(self, tries=5):
        """A reading that isn't obviously corrupt. None if all attempts fail."""
        for _ in range(tries):
            v = self._read_raw_once()
            if v is not None and v not in (-1.0, 0.0) and abs(v) < 0x7FFFFF:
                return v
            self.discarded += 1
            self._reset()
        return None

    def read_raw(self, samples=5):
        if not self._started:
            self.start()
        values = list(self._recent)[-max(1, samples):]
        if not values:
            raise RuntimeError(self.last_error or "No reading from the load cell yet.")
        return statistics.median(values)

    def grams(self, samples=3):
        if self.scale_factor == 0:
            return 0.0
        return (self.read_raw(samples) - self.offset) / self.scale_factor

    def tare(self):
        self.offset = self.read_raw(samples=15)
        return self.offset

    def calibrate(self, known_grams):
        delta = self.read_raw(samples=15) - self.offset
        if known_grams <= 0 or delta == 0:
            raise ValueError("Put a known weight on the platform first, then calibrate.")
        self.scale_factor = delta / known_grams
        self.calibrated = True
        return self.scale_factor

    def release(self):
        chip = getattr(self, "_chip", None)
        if chip is None:
            return
        for pin in (HX711_CLOCK_PIN, HX711_DATA_PIN):
            try:
                lgpio.gpio_free(chip, pin)
            except Exception:
                pass
        try:
            lgpio.gpiochip_close(chip)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Timed test pour
# ---------------------------------------------------------------------------

class PourTest:
    """Runs one line for a fixed time and reports what landed on the platform."""

    def __init__(self, pumps, scale):
        self.pumps = pumps
        self.scale = scale
        self._lock = threading.Lock()
        self.running = False
        self.abort = False
        self.line = None
        self.duration = 0.0
        self.started_at = 0.0
        self.start_grams = 0.0
        self.result = None        # kept until the next test starts

    def start(self, line, seconds, speed=1.0):
        with self._lock:
            if self.running:
                raise RuntimeError("A test is already running.")
            if not 1 <= line <= self.pumps.count:
                raise ValueError("No such line.")
            seconds = max(0.5, min(float(seconds), MAX_POUR_SECONDS))
            self.running = True
            self.abort = False
            self.line = line
            self.duration = seconds
            self.result = None

        threading.Thread(target=self._run, args=(line, seconds, speed),
                         daemon=True).start()

    def _run(self, line, seconds, speed):
        try:
            self.start_grams = self._safe_grams()
            self.started_at = time.time()
            self.pumps.set(line, speed)

            while time.time() - self.started_at < seconds:
                if self.abort:
                    break
                time.sleep(0.02)

            self.pumps.set(line, 0.0)
            elapsed = time.time() - self.started_at

            # Let the platform settle before reading the final weight.
            time.sleep(0.6)
            delta = self._safe_grams() - self.start_grams

            # Under a second is too short for the rate to mean anything — the
            # pump is still spinning up and the platform hasn't settled.
            rate = round(delta / elapsed * 60.0, 1) if elapsed >= 1.0 else None

            self.result = {
                "line": line,
                "seconds": round(elapsed, 2),
                "grams": round(delta, 1),
                "ml_per_min": rate,
                "aborted": self.abort,
                "at": time.strftime("%H:%M:%S"),
            }
        except Exception as e:
            self.pumps.set(line, 0.0)
            self.result = {"line": line, "error": str(e)}
        finally:
            self.running = False

    def stop(self):
        self.abort = True

    def _safe_grams(self):
        try:
            return self.scale.grams(samples=9)
        except Exception:
            return 0.0

    def status(self):
        if not self.running:
            return {"running": False, "result": self.result}
        return {
            "running": True,
            "line": self.line,
            "elapsed": round(time.time() - self.started_at, 2),
            "duration": self.duration,
            "start_grams": round(self.start_grams, 1),
            "result": None,
        }


# ---------------------------------------------------------------------------
# Wiring it together
# ---------------------------------------------------------------------------

pumps = Pumps()
scale = Scale()
scale.start()
pour_test = PourTest(pumps, scale)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

PAGE = r"""
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bench test</title>
<style>
  :root{
    --panel:#E8E6E1;
    --card:#F6F5F2;
    --ink:#1A1A18;
    --muted:#6E6C66;
    --rule:#CFCCC4;
    --live:#C77B22;
    --halt:#8A2B18;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  *{box-sizing:border-box}
  body{background:var(--panel);color:var(--ink);font-family:var(--sans);
       margin:0 auto;padding:1.25rem;max-width:560px}

  .strip{display:flex;justify-content:space-between;align-items:center;gap:1rem;
         font-family:var(--mono);font-size:.7rem;letter-spacing:.08em;
         text-transform:uppercase;color:var(--muted);
         border-bottom:1px solid var(--rule);padding-bottom:.6rem;margin-bottom:1.5rem}

  h2{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;
     text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 .6rem}

  section{margin-bottom:2rem}

  .readout{background:var(--card);border:1px solid var(--rule);border-radius:4px;
           padding:1.1rem 1.25rem}
  .grams{font-family:var(--mono);font-size:3.4rem;font-weight:500;line-height:1;
         font-variant-numeric:tabular-nums;letter-spacing:-.02em;
         display:flex;align-items:baseline;gap:.4rem}
  .grams small{font-size:1rem;color:var(--muted);font-weight:400}
  .grams.fault{color:var(--halt);font-size:1.1rem;letter-spacing:0}
  .sub{font-family:var(--mono);font-size:.72rem;color:var(--muted);
       margin-top:.5rem;min-height:1.1em}

  .bar{height:3px;background:var(--rule);border-radius:2px;margin-top:.9rem;
       overflow:hidden;display:none}
  .bar.on{display:block}
  .bar i{display:block;height:100%;width:0;background:var(--live)}

  .lines{display:flex;flex-direction:column;gap:2px}
  .line{display:flex;align-items:center;gap:.9rem;background:var(--card);
        border:1px solid var(--rule);padding:.55rem .8rem}
  .line:first-child{border-radius:4px 4px 0 0}
  .line:last-child{border-radius:0 0 4px 4px}
  .line b{font-family:var(--mono);font-size:.8rem;font-weight:600;width:1.4rem;
          color:var(--muted)}
  .line .pin{font-family:var(--mono);font-size:.68rem;color:var(--muted);flex:1}
  .line.live{border-color:var(--live);background:#FBF3E8}
  .line.live b{color:var(--live)}

  button{font-family:var(--sans);font-size:.85rem;padding:.45rem .9rem;
         border:1px solid var(--rule);background:var(--card);color:var(--ink);
         border-radius:3px;cursor:pointer}
  button:hover{border-color:var(--muted)}
  button:focus-visible{outline:2px solid var(--live);outline-offset:2px}
  button[aria-pressed="true"]{background:var(--live);border-color:var(--live);
                              color:#fff;font-weight:600}
  button:disabled{opacity:.4;cursor:default}

  .row{display:flex;gap:.4rem;margin-top:.9rem;flex-wrap:wrap}
  .row button{flex:1;min-width:7rem}

  .halt{width:100%;padding:.85rem;margin-top:1.5rem;font-weight:600;
        border-color:var(--halt);color:var(--halt);background:none;
        font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;
        font-size:.78rem}
  .halt:hover{background:var(--halt);color:var(--panel);border-color:var(--halt)}

  .fields{display:flex;gap:.5rem;align-items:flex-end;flex-wrap:wrap}
  label{display:block;font-family:var(--mono);font-size:.66rem;
        text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
        margin-bottom:.25rem}
  select,input{font-family:var(--mono);font-size:.9rem;padding:.42rem .5rem;
               border:1px solid var(--rule);border-radius:3px;
               background:var(--card);color:var(--ink)}
  input{width:5rem}

  table{width:100%;border-collapse:collapse;font-family:var(--mono);
        font-size:.76rem;margin-top:1rem}
  th{text-align:left;font-weight:500;color:var(--muted);font-size:.66rem;
     text-transform:uppercase;letter-spacing:.08em;
     border-bottom:1px solid var(--rule);padding:.35rem .4rem}
  td{padding:.35rem .4rem;border-bottom:1px solid var(--rule);
     font-variant-numeric:tabular-nums}
  .note{font-family:var(--mono);font-size:.72rem;color:var(--muted);margin-top:.9rem}
</style>
</head><body>

<div class="strip">
  <span id="mode">—</span>
  <button id="pol" onclick="flipPolarity()" style="font-family:var(--mono);
    font-size:.66rem;letter-spacing:.06em;text-transform:uppercase;padding:.3rem .6rem">
    —</button>
</div>

<section>
  <h2>Platform</h2>
  <div class="readout">
    <div class="grams"><span id="w">—</span><small id="unit">g</small></div>
    <div class="sub" id="wsub"></div>
    <div class="bar" id="bar"><i id="barfill"></i></div>
  </div>
  <div class="row">
    <button onclick="tare()">Set zero</button>
    <button onclick="calibrate()">Calibrate</button>
  </div>
</section>

<section>
  <h2>Lines</h2>
  <div class="lines" id="lines"></div>
</section>

<section>
  <h2>Timed test</h2>
  <div class="fields">
    <div>
      <label for="tline">Line</label>
      <select id="tline"></select>
    </div>
    <div>
      <label for="tsecs">Seconds</label>
      <input id="tsecs" type="number" value="5" min="0.5" max="30" step="0.5">
    </div>
    <button id="trun" onclick="runTest()">Run test</button>
  </div>
  <div id="results"><p class="note">No tests run yet. Put a glass on the platform, set zero, then run one.</p></div>
</section>

<button class="halt" onclick="stopAll()">Stop everything</button>

<script>
const COUNT = {count};
const state = {};
const results = [];
let activeHigh = true;
let testing = false;

const linesEl = document.getElementById('lines');
const sel = document.getElementById('tline');
for (let i = 1; i <= COUNT; i++) {
  const row = document.createElement('div');
  row.className = 'line';
  row.id = 'row' + i;
  row.innerHTML =
    '<b>' + i + '</b>' +
    '<span class="pin" id="pin' + i + '"></span>' +
    '<button id="b' + i + '" aria-pressed="false" onclick="toggle(' + i + ')">Off</button>';
  linesEl.appendChild(row);

  const opt = document.createElement('option');
  opt.value = i; opt.textContent = 'Line ' + i;
  sel.appendChild(opt);
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  });
  return r.json();
}

function paint(line, on) {
  const b = document.getElementById('b' + line);
  b.textContent = on ? 'Running' : 'Off';
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  document.getElementById('row' + line).className = on ? 'line live' : 'line';
}

async function toggle(line) {
  const next = !state[line];
  const d = await post('/api/pump', {line: line, on: next});
  if (!d.ok) { alert(d.error); return; }
  state[line] = next;
  paint(line, next);
}

async function stopAll() {
  await post('/api/stop');
  for (let i = 1; i <= COUNT; i++) { state[i] = false; paint(i, false); }
}

async function flipPolarity() {
  const d = await post('/api/polarity', {active_high: !activeHigh});
  activeHigh = d.active_high;
  paintPolarity();
}

function paintPolarity() {
  document.getElementById('pol').textContent =
    'Board: ' + (activeHigh ? 'active high' : 'active low');
}

async function tare() {
  const d = await post('/api/tare');
  if (!d.ok) alert(d.error);
}

async function calibrate() {
  const g = prompt('What does the weight on the platform actually weigh, in grams?');
  if (!g) return;
  const d = await post('/api/calibrate', {grams: parseFloat(g)});
  if (!d.ok) alert(d.error);
}

async function runTest() {
  const line = parseInt(document.getElementById('tline').value, 10);
  const secs = parseFloat(document.getElementById('tsecs').value);
  const d = await post('/api/test', {line: line, seconds: secs});
  if (!d.ok) { alert(d.error); return; }
  testing = true;
  document.getElementById('trun').disabled = true;
  document.getElementById('bar').className = 'bar on';
}

function addResult(r) {
  if (r.error) { alert('Test failed: ' + r.error); return; }
  results.unshift(r);
  const rows = results.map(function (x) {
    const rate = (x.ml_per_min === null) ? '——' : x.ml_per_min;
    return '<tr><td>' + x.line + '</td><td>' + x.seconds + '</td><td>' +
           x.grams + '</td><td>' + rate + '</td><td>' +
           (x.aborted ? 'stopped early' : x.at) + '</td></tr>';
  }).join('');
  document.getElementById('results').innerHTML =
    '<table><thead><tr><th>Line</th><th>Secs</th><th>Grams</th>' +
    '<th>ml/min</th><th>When</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();

    const wEl = document.getElementById('w');
    const uEl = document.getElementById('unit');
    if (d.grams === null) {
      wEl.textContent = d.error || 'No reading';
      wEl.parentNode.className = 'grams fault';
      uEl.style.display = 'none';
    } else {
      wEl.textContent = d.grams.toFixed(1);
      wEl.parentNode.className = 'grams';
      uEl.style.display = '';
      uEl.textContent = d.calibrated ? 'g' : 'raw';
    }

    document.getElementById('mode').textContent =
      COUNT + ' lines · ' + (d.calibrated ? 'calibrated' : 'not calibrated') +
      (d.discarded > 0 ? ' · ' + d.discarded + ' reads dropped' : '');

    if (d.active_high !== activeHigh) { activeHigh = d.active_high; paintPolarity(); }

    for (let i = 1; i <= COUNT; i++) {
      const on = d.pumps[i - 1] > 0;
      if (state[i] !== on) { state[i] = on; paint(i, on); }
      document.getElementById('pin' + i).textContent = 'BCM ' + d.pins[i - 1];
    }

    const t = d.test;
    if (t.running) {
      document.getElementById('barfill').style.width =
        Math.min(100, t.elapsed / t.duration * 100) + '%';
      document.getElementById('wsub').textContent =
        'Testing line ' + t.line + ' · ' + t.elapsed.toFixed(1) + ' of ' +
        t.duration + 's' +
        (d.grams === null ? '' : ' · ' + (d.grams - t.start_grams).toFixed(1) + ' so far');
    } else if (testing) {
      testing = false;
      document.getElementById('trun').disabled = false;
      document.getElementById('bar').className = 'bar';
      document.getElementById('barfill').style.width = '0';
      document.getElementById('wsub').textContent = '';
      if (t.result) addResult(t.result);
    } else {
      document.getElementById('wsub').textContent =
        d.calibrated ? '' : 'Set zero, then calibrate, before the numbers mean grams.';
    }
  } catch (e) { /* server restarting, try again */ }
  setTimeout(poll, 250);
}

paintPolarity();
poll();
</script>
</body></html>
"""


@app.route("/")
def index():
    return PAGE.replace("{count}", str(pumps.count))


@app.route("/api/state")
def state():
    try:
        grams = round(scale.grams(samples=3), 1)
        err = None
    except Exception as e:
        grams, err = None, str(e)
    return jsonify({
        "grams": grams,
        "error": err,
        "calibrated": scale.calibrated,
        "discarded": scale.discarded,
        "pumps": pumps.states(),
        "pins": pumps.pins,
        "active_high": pumps.active_high,
        "test": pour_test.status(),
    })


@app.route("/api/pump", methods=["POST"])
def pump():
    if pour_test.running:
        return jsonify({"ok": False, "error": "A timed test is running."}), 409
    d = request.get_json(force=True)
    pumps.set(int(d["line"]), 1.0 if d.get("on") else 0.0)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    pour_test.stop()
    pumps.all_off()
    return jsonify({"ok": True})


@app.route("/api/polarity", methods=["POST"])
def polarity():
    d = request.get_json(force=True)
    pumps.set_polarity(bool(d.get("active_high")))
    return jsonify({"ok": True, "active_high": pumps.active_high})


@app.route("/api/tare", methods=["POST"])
def tare():
    try:
        return jsonify({"ok": True, "offset": scale.tare()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/calibrate", methods=["POST"])
def calibrate():
    try:
        d = request.get_json(force=True)
        return jsonify({"ok": True, "scale_factor": scale.calibrate(float(d["grams"]))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/test", methods=["POST"])
def test():
    try:
        d = request.get_json(force=True)
        pour_test.start(int(d["line"]), float(d["seconds"]))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


if __name__ == "__main__":
    print("Pump pins: %s" % ", ".join("BCM %d" % p for p in PUMP_PINS))
    print("Board polarity: %s (flip it in the page if the pumps run backwards)"
          % ("active high" if ACTIVE_HIGH else "active low"))
    print("Open http://localhost:%d" % PORT)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        pumps.all_off()
        pumps.release()
        scale.release()
        print("\nAll pumps off, pins released.")
