"""
app.py — the web server.

Run it:      python3 app.py
Then open:   http://localhost:5000   (or http://<pi-address>:5000 from your phone)
"""

import json
import os
import threading
from flask import Flask, jsonify, request, send_from_directory

import hardware
from hardware import Pumps, Scale, start_mock_flow
from pour import Bartender

HERE = os.path.dirname(os.path.abspath(__file__))
BOTTLES_FILE = os.path.join(HERE, "bottles.json")
RECIPES_FILE = os.path.join(HERE, "recipes.json")
CALIBRATION_FILE = os.path.join(HERE, "calibration.json")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Everything that lives on disk: what's on each line, and the recipes."""

    def __init__(self):
        self._lock = threading.Lock()
        self.bottles = self._load(BOTTLES_FILE, [])
        self.recipes = self._load(RECIPES_FILE, [])

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
pumps = Pumps()
scale = Scale()

# Load saved calibration
try:
    with open(CALIBRATION_FILE) as f:
        cal = json.load(f)
        scale.offset = cal.get("offset", 0.0)
        scale.scale_factor = cal.get("scale_factor", 1.0)
except (FileNotFoundError, json.JSONDecodeError):
    pass

if hardware.MOCK:
    scale.scale_factor = 1000.0
    scale.mock_set_glass(180.0)   # pretend there's a glass on the tray
    start_mock_flow(pumps, scale)

bartender = Bartender(pumps, scale, config)

app = Flask(__name__, static_folder="static")


def save_calibration():
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


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    grams = float(request.json["grams"])
    try:
        scale.calibrate(grams)
        save_calibration()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "scale_factor": scale.scale_factor})


if __name__ == "__main__":
    print("Mock mode" if hardware.MOCK else "Hardware mode")
    print("Open http://localhost:5000")
    try:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    finally:
        pumps.all_off()
