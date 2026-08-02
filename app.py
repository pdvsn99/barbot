"""
app.py — the web server.

Run it:      python3 app.py
Then open:   http://barbot.local   (or http://localhost from the Pi itself)

Serves on port 80 so there's no ":5000" to remember. That's a privileged port,
so running it by hand off the Pi needs either root or an override:

    PORT=5000 python3 app.py

On the Pi, systemd grants the one capability needed instead — see bootstrap.sh.
"""

import json
import os
import socket
import threading
from flask import Flask, jsonify, request, send_from_directory

import hardware
from hardware import Pumps, Scale, start_mock_flow
from pour import Bartender

HERE = os.path.dirname(os.path.abspath(__file__))
BOTTLES_FILE = os.path.join(HERE, "bottles.json")
BOTTLES_DEFAULT = os.path.join(HERE, "bottles.default.json")
RECIPES_FILE = os.path.join(HERE, "recipes.json")
CALIBRATION_FILE = os.path.join(HERE, "calibration.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")

# bottles.json holds live state — what's loaded, how much is left — so it is
# NOT tracked in git, or every pull would collide with it. On a fresh machine
# we seed it from the tracked default.
if not os.path.exists(BOTTLES_FILE) and os.path.exists(BOTTLES_DEFAULT):
    import shutil
    shutil.copy(BOTTLES_DEFAULT, BOTTLES_FILE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Everything that lives on disk: what's on each line, and the recipes."""

    def __init__(self):
        self._lock = threading.Lock()
        self.bottles = self._load(BOTTLES_FILE, [])
        self.recipes = self._load(RECIPES_FILE, [])
        # Set once the pumps exist. Lines beyond this aren't wired up yet, so
        # anything sitting on them doesn't count as loaded.
        self.max_line = None

    @staticmethod
    def _load(path, fallback):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _save(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def bottle(self, line):
        for b in self.bottles:
            if b["line"] == line:
                return b
        return {"line": line, "ingredient": None}

    def save_bottles(self, bottles):
        with self._lock:
            self.bottles = bottles
            self._save(BOTTLES_FILE, bottles)

    def save_recipes(self, recipes):
        with self._lock:
            self.recipes = recipes
            self._save(RECIPES_FILE, recipes)

    def loaded_ingredients(self):
        return {
            b["ingredient"].lower(): b
            for b in self.bottles
            if b.get("ingredient")
            and (self.max_line is None or b["line"] <= self.max_line)
        }

    def consume(self, line, ml):
        """Decrement the remaining volume estimate after a pour."""
        with self._lock:
            for b in self.bottles:
                if b["line"] == line and b.get("remaining_ml") is not None:
                    b["remaining_ml"] = max(0, round(b["remaining_ml"] - ml, 1))
            self._save(BOTTLES_FILE, self.bottles)

    def split_recipe(self, recipe):
        """
        Returns (pumped, manual).
        `pumped` is what the machine can do; `manual` is what you add by hand.
        """
        loaded = self.loaded_ingredients()
        pumped, manual = [], []
        for item in recipe["ingredients"]:
            key = item["ingredient"].lower()
            bottle = loaded.get(key)
            if bottle and not item.get("manual"):
                pumped.append({
                    "line": bottle["line"],
                    "ingredient": bottle["ingredient"],
                    "ml": float(item["ml"]),
                    "density": float(bottle.get("density", 0.94)),
                })
            else:
                manual.append(item.get("note") or f'{item["ingredient"]}'
                              + (f' — {item["ml"]}ml' if item.get("ml") else ""))
        return pumped, manual

    def can_make(self, recipe):
        """A recipe is makeable if every non-manual ingredient is loaded."""
        loaded = self.loaded_ingredients()
        for item in recipe["ingredients"]:
            if item.get("manual"):
                continue
            if item["ingredient"].lower() not in loaded:
                return False
        return True


# ---------------------------------------------------------------------------
# Wire everything up
# ---------------------------------------------------------------------------

config = Config()

# settings.json holds the mock/hardware choice made in the Care tab. It's
# untracked, which is the whole point: the Pi resets tracked files to match
# GitHub on every update, so the MOCK constant in hardware.py can't remember
# anything. This can.
settings = Config._load(SETTINGS_FILE, {})
if isinstance(settings.get("mock"), bool):
    try:
        hardware.set_mock(settings["mock"])
    except Exception as e:
        print(f"Couldn't start in {'mock' if settings['mock'] else 'hardware'} "
              f"mode ({e}) — staying in {'mock' if hardware.MOCK else 'hardware'}.")


def save_settings():
    Config._save(SETTINGS_FILE, {"mock": hardware.MOCK})


pumps = Pumps()
scale = Scale()
config.max_line = pumps.count

# Calibration is per mode. A tare done against the fake scale would be
# nonsense on the real one, so the two are kept apart and only the real one is
# written to disk.
MOCK_CALIBRATION = {"offset": 0.0, "scale_factor": 1000.0}


def load_calibration():
    cal = Config._load(CALIBRATION_FILE, {})
    try:
        return {"offset": float(cal.get("offset", 0.0)),
                "scale_factor": float(cal.get("scale_factor", 1.0))}
    except (TypeError, ValueError):
        return {"offset": 0.0, "scale_factor": 1.0}


calibrations = {True: dict(MOCK_CALIBRATION), False: load_calibration()}


def remember_calibration():
    """Stash the live figures against whichever mode we're in."""
    calibrations[hardware.MOCK] = {"offset": scale.offset,
                                   "scale_factor": scale.scale_factor}


def apply_calibration():
    cal = calibrations[hardware.MOCK]
    scale.offset = cal["offset"]
    scale.scale_factor = cal["scale_factor"]


apply_calibration()
if hardware.MOCK:
    scale.mock_set_glass(180.0)   # pretend there's a glass on the tray
start_mock_flow(pumps, scale)     # idles unless the mode is mock

scale.start_reader()

bartender = Bartender(pumps, scale, config)


# A real glass gets emptied and put back. The fake one doesn't, so without
# this the tray reading climbs forever as people play with the demo.
def _reset_fake_glass():
    import time as _t
    while True:
        _t.sleep(5)
        try:
            if hardware.MOCK and not bartender.status["busy"] and scale.grams(1) > 400:
                scale.mock_set_glass(180.0)
                for b in config.bottles:
                    b["remaining_ml"] = b.get("bottle_ml", 700)
        except Exception:
            pass   # mid mode-switch, most likely. Try again in five seconds.


threading.Thread(target=_reset_fake_glass, daemon=True).start()

app = Flask(__name__, static_folder="static")


def save_calibration():
    remember_calibration()
    if hardware.MOCK:
        return   # don't let a demo overwrite the machine's real numbers
    with open(CALIBRATION_FILE, "w") as f:
        json.dump({"offset": scale.offset, "scale_factor": scale.scale_factor}, f, indent=2)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def state():
    recipes = []
    for r in config.recipes:
        pumped, manual = config.split_recipe(r)
        recipes.append({**r, "available": config.can_make(r),
                        "manual_steps": manual, "pump_steps": len(pumped)})
    return jsonify({
        "mock": hardware.MOCK,
        "lines": pumps.count,
        "bottles": config.bottles,
        "recipes": recipes,
        "status": bartender.status,
        "weight_g": bartender.safe_weight(),
        "calibrated": scale.scale_factor not in (0, 1.0),
    })


@app.route("/api/pour", methods=["POST"])
def api_pour():
    recipe_id = request.json.get("id")
    recipe = next((r for r in config.recipes if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"error": "No recipe with that name."}), 404
    if not config.can_make(recipe):
        return jsonify({"error": "Not everything for that is loaded."}), 400
    try:
        bartender.make_drink(recipe)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/api/custom", methods=["POST"])
def api_custom():
    """Pour a single ingredient by name — handy for a straight measure."""
    data = request.json
    recipe = {
        "id": "custom",
        "name": data.get("name", "Custom pour"),
        "ingredients": [{"ingredient": data["ingredient"], "ml": float(data["ml"])}],
    }
    if not config.can_make(recipe):
        return jsonify({"error": "That isn't loaded on any line."}), 400
    bartender.make_drink(recipe)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    bartender.abort()
    pumps.all_off()
    return jsonify({"ok": True})


@app.route("/api/prime", methods=["POST"])
def api_prime():
    line = int(request.json["line"])
    seconds = float(request.json.get("seconds", 6))
    try:
        bartender.prime(line, seconds)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/api/clean", methods=["POST"])
def api_clean():
    try:
        bartender.clean()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/api/bottles", methods=["POST"])
def api_bottles():
    config.save_bottles(request.json["bottles"])
    return jsonify({"ok": True})


@app.route("/api/recipes", methods=["POST"])
def api_recipes():
    config.save_recipes(request.json["recipes"])
    return jsonify({"ok": True})


@app.route("/api/tare", methods=["POST"])
def api_tare():
    try:
        scale.tare()
        save_calibration()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "offset": scale.offset})


@app.route("/api/mock", methods=["POST"])
def api_mock():
    """Switch between fake hardware and the real thing, from the Care tab."""
    want = bool(request.json.get("mock"))
    if want == hardware.MOCK:
        return jsonify({"ok": True, "mock": hardware.MOCK})
    if bartender.busy():
        return jsonify({"error": "Finish what it's doing first."}), 409

    pumps.all_off()
    remember_calibration()
    try:
        hardware.set_mock(want)
    except ImportError:
        return jsonify({"error": "No gpiozero here — this isn't a Pi, so it "
                                 "can only run in mock mode."}), 409
    except Exception as e:
        return jsonify({"error": f"Couldn't switch to hardware: {e}"}), 500

    apply_calibration()
    if hardware.MOCK:
        scale.mock_set_glass(180.0)
    save_settings()
    return jsonify({"ok": True, "mock": hardware.MOCK})


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    grams = float(request.json["grams"])
    try:
        scale.calibrate(grams)
        save_calibration()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "scale_factor": scale.scale_factor})


FALLBACK_PORT = 5000


def port_problem(port):
    """
    Why we can't have this port, in words, or None if we can.

    Worth asking up front: werkzeug reports a refused bind as a bare
    "Permission denied" and exits, which doesn't hint at the fix.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
    except PermissionError:
        return f"Port {port} needs privileges this process hasn't got."
    except OSError as e:
        return f"Can't listen on port {port}: {e}"
    finally:
        probe.close()
    return None


if __name__ == "__main__":
    asked_for = os.environ.get("PORT")
    port = int(asked_for or 80)
    print("Mock mode" if hardware.MOCK else "Hardware mode")

    problem = port_problem(port)
    if problem and not asked_for and port_problem(FALLBACK_PORT) is None:
        # A Pi set up before port 80 was a thing won't have the capability in
        # its systemd unit, and only bootstrap.sh can put it there. Coming up
        # on the old port beats not coming up at all — systemd would just
        # restart us into the same wall until the update rolled back.
        print(f"{problem}\nUsing port {FALLBACK_PORT} instead. Re-run "
              f"bootstrap.sh on the Pi for port 80 and barbot.local.")
        port, problem = FALLBACK_PORT, None

    if problem:
        print(f"{problem}\nPick another one with:  PORT=5000 python3 app.py")
        raise SystemExit(1)

    print(f"Open http://localhost{'' if port == 80 else ':' + str(port)}")
    try:
        app.run(host="0.0.0.0", port=port, threaded=True)
    finally:
        pumps.all_off()
