"""
test_app.py — a bench-test web page. Not the real app.

Toggle pumps by hand, watch the scale live, zero and calibrate it. Run this
instead of app.py while you're still wiring things up.

    sudo systemctl stop barbot     (frees the GPIO pins first)
    cd ~/barbot
    python3 test_app.py

Then open http://<pi-address>:5001
"""

from flask import Flask, jsonify, request
import hardware
from hardware import Pumps, Scale

pumps = Pumps()
scale = Scale()
scale.scale_factor = 1000.0 if hardware.MOCK else scale.scale_factor
scale.start_reader()

app = Flask(__name__)

PAGE = """
<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bench test</title>
<style>
  body{font-family:-apple-system,sans-serif;background:#101614;color:#EDE6D6;
       max-width:480px;margin:0 auto;padding:1.5rem}
  h1{font-size:1.1rem;color:#7E8F87;text-transform:uppercase;letter-spacing:.1em}
  .weight{font-size:4rem;font-family:monospace;margin:1rem 0}
  .line{display:flex;align-items:center;gap:.75rem;background:#18211E;
        border:1px solid #2A3833;border-radius:10px;padding:.8rem 1rem;margin-bottom:.5rem}
  .line span{width:1.5rem}
  button{font-size:1rem;padding:.6rem 1.1rem;border-radius:8px;border:1px solid #2A3833;
         background:#1F2B26;color:#EDE6D6}
  button.on{background:#E9A13B;color:#101614;border-color:#E9A13B}
  button.stop{background:none;border-color:#D2624A;color:#D2624A;width:100%;
              padding:.9rem;margin-top:1.5rem;font-weight:bold}
  .row{display:flex;gap:.5rem;margin:1rem 0}
  .row button{flex:1}
</style></head><body>
<h1>Tray weight</h1>
<div class="weight" id="w">—</div>
<div class="row">
  <button onclick="post('/api/tare')">Zero</button>
  <button onclick="cal()">Calibrate</button>
</div>

<h1 style="margin-top:2rem">Pump lines</h1>
<div id="lines"></div>

<button class="stop" onclick="post('/api/all_off')">STOP ALL PUMPS</button>

<script>
const LINES = {count};
const lines = document.getElementById('lines');
for (let i = 1; i <= LINES; i++) {
  const row = document.createElement('div');
  row.className = 'line';
  row.innerHTML = `<span>${i}</span><button id="b${i}" onclick="toggle(${i})">Off</button>`;
  lines.appendChild(row);
}

async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify(body||{})});
  return r.json();
}

let state = {};
async function toggle(line) {
  const now = !(state[line]);
  await post('/api/pump', {line, on: now});
  state[line] = now;
  const b = document.getElementById('b' + line);
  b.textContent = now ? 'On' : 'Off';
  b.className = now ? 'on' : '';
}

function cal() {
  const g = prompt('Weight on the tray right now, in grams?');
  if (g) post('/api/calibrate', {grams: parseFloat(g)}).then(d => {
    if (!d.ok) alert(d.error);
  });
}

async function poll() {
  try {
    const r = await fetch('/api/weight');
    const d = await r.json();
    document.getElementById('w').textContent =
      d.grams === null ? 'no reading' : d.grams.toFixed(1) + ' g';
  } catch (e) {}
  setTimeout(poll, 300);
}
poll();
</script>
</body></html>
"""

@app.route("/")
def index():
    return PAGE.replace("{count}", str(pumps.count))

@app.route("/api/weight")
def weight():
    try:
        return jsonify({"grams": round(scale.grams(samples=3), 1)})
    except Exception:
        return jsonify({"grams": None})

@app.route("/api/pump", methods=["POST"])
def pump():
    d = request.json
    pumps.set(int(d["line"]), 1.0 if d["on"] else 0.0)
    return jsonify({"ok": True})

@app.route("/api/all_off", methods=["POST"])
def all_off():
    pumps.all_off()
    return jsonify({"ok": True})

@app.route("/api/tare", methods=["POST"])
def tare():
    scale.tare()
    return jsonify({"ok": True, "offset": scale.offset})

@app.route("/api/calibrate", methods=["POST"])
def calibrate():
    try:
        scale.calibrate(float(request.json["grams"]))
        return jsonify({"ok": True, "scale_factor": scale.scale_factor})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

if __name__ == "__main__":
    print("Mock mode" if hardware.MOCK else "Hardware mode")
    print("Open http://localhost:5001")
    try:
        app.run(host="0.0.0.0", port=5001, threaded=True)
    finally:
        pumps.all_off()
