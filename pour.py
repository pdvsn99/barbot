"""
pour.py — the bit that actually makes a drink.

Everything is controlled by weight, not time. We tare the scale with the empty
glass on it, run a pump, and watch the reading climb until it hits the target.
"""

import time
import threading


class PourAborted(Exception):
    pass


class Bartender:
    """Runs one job at a time. Status is readable from any thread."""

    # Safety limits
    MIN_GLASS_GRAMS = 40.0   # refuse to pour if nothing is on the scale
    STALL_SECONDS = 4.0      # no meaningful weight gain for this long = problem
    STALL_GRAMS = 1.5        # what counts as meaningful gain
    MAX_SECONDS_PER_POUR = 90.0
    SLOW_AT = 0.85           # drop to a crawl for the last 15% of a pour
    SLOW_SPEED = 0.35

    def __init__(self, pumps, scale, config):
        self.pumps = pumps
        self.scale = scale
        self.config = config          # the Config object from app.py
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self.status = {
            "busy": False,
            "job": None,            # "drink" | "prime" | "clean"
            "drink": None,
            "step": None,           # human-readable current action
            "ingredient": None,
            "target_g": 0.0,
            "poured_g": 0.0,
            "steps_total": 0,
            "steps_done": 0,
            "manual": [],           # things you need to add yourself
            "error": None,
            "finished_at": None,
        }

    # -- helpers ------------------------------------------------------------

    def _set(self, **kwargs):
        self.status.update(kwargs)

    def busy(self):
        return self.status["busy"]

    def abort(self):
        self._abort.set()

    def safe_weight(self):
        """Current weight, or None if the scale is unhappy."""
        try:
            return round(self.scale.grams(samples=3), 1)
        except Exception:
            return None

    # -- the core loop ------------------------------------------------------

    def _pour_ingredient(self, line, grams_target, label):
        """Run one pump until `grams_target` has landed in the glass."""
        bottle = self.config.bottle(line)
        overshoot = float(bottle.get("overshoot_g", 1.0))
        stop_at = max(0.5, grams_target - overshoot)

        start = self.scale.grams(samples=8)
        started = time.time()
        last_gain_time = started
        last_gain = 0.0

        self._set(ingredient=label, target_g=round(grams_target, 1), poured_g=0.0)
        self.pumps.set(line, 1.0)

        try:
            while True:
                if self._abort.is_set():
                    raise PourAborted("Stopped by you.")

                now = self.scale.grams(samples=2)
                gained = max(0.0, now - start)
                self._set(poured_g=round(gained, 1))

                if gained >= stop_at:
                    break

                if gained >= grams_target * self.SLOW_AT:
                    self.pumps.set(line, self.SLOW_SPEED)

                if gained - last_gain >= self.STALL_GRAMS:
                    last_gain = gained
                    last_gain_time = time.time()
                elif time.time() - last_gain_time > self.STALL_SECONDS:
                    raise PourAborted(
                        f"{label} stopped flowing. Check the bottle isn't empty "
                        f"and the tube isn't kinked, then prime line {line}."
                    )

                if time.time() - started > self.MAX_SECONDS_PER_POUR:
                    raise PourAborted(f"{label} took too long. Stopped for safety.")

                time.sleep(0.05)
        finally:
            self.pumps.off(line)

        # Let the last of it drain, then record what actually landed.
        time.sleep(0.6)
        final = max(0.0, self.scale.grams(samples=8) - start)
        self._set(poured_g=round(final, 1))
        self.config.consume(line, final / max(0.1, float(bottle.get("density", 0.94))))
        return final

    # -- jobs ---------------------------------------------------------------

    def make_drink(self, recipe):
        with self._lock:
            if self.status["busy"]:
                raise RuntimeError("Already making something.")
            self._abort.clear()

            pumped, manual = self.config.split_recipe(recipe)
            self._set(
                busy=True, job="drink", drink=recipe["name"], error=None,
                steps_total=len(pumped), steps_done=0, manual=manual,
                step="Checking the glass", finished_at=None,
            )

        def run():
            try:
                weight = self.scale.grams(samples=10)
                if weight < self.MIN_GLASS_GRAMS:
                    raise PourAborted("Put a glass on the tray first.")

                for i, item in enumerate(pumped, start=1):
                    self._set(step=f"Pouring {item['ingredient']}", steps_done=i - 1)
                    grams = item["ml"] * item["density"]
                    self._pour_ingredient(item["line"], grams, item["ingredient"])
                    self._set(steps_done=i)

                self._set(step="Done", ingredient=None, finished_at=time.time())
            except PourAborted as e:
                self.pumps.all_off()
                self._set(step="Stopped", error=str(e))
            except Exception as e:
                self.pumps.all_off()
                self._set(step="Stopped", error=f"Something went wrong: {e}")
            finally:
                self.pumps.all_off()
                self._set(busy=False)

        threading.Thread(target=run, daemon=True).start()

    def prime(self, line, seconds=6.0):
        """Push liquid up to the nozzle after changing a bottle."""
        with self._lock:
            if self.status["busy"]:
                raise RuntimeError("Already busy.")
            self._abort.clear()
            bottle = self.config.bottle(line)
            self._set(busy=True, job="prime", drink=None, error=None,
                      step=f"Priming line {line} — {bottle.get('ingredient', 'empty')}",
                      ingredient=bottle.get("ingredient"), steps_total=1, steps_done=0,
                      target_g=0, poured_g=0, manual=[], finished_at=None)

        def run():
            try:
                self.pumps.on(line)
                end = time.time() + seconds
                while time.time() < end:
                    if self._abort.is_set():
                        break
                    time.sleep(0.05)
                self._set(step="Primed", steps_done=1, finished_at=time.time())
            finally:
                self.pumps.all_off()
                self._set(busy=False)

        threading.Thread(target=run, daemon=True).start()

    def clean(self, seconds=25.0, purge_seconds=12.0):
        """
        Two stages. Put all eight tubes in a jug of warm water for the first,
        then lift them out into the air for the purge.
        """
        with self._lock:
            if self.status["busy"]:
                raise RuntimeError("Already busy.")
            self._abort.clear()
            self._set(busy=True, job="clean", drink=None, error=None,
                      step="Flushing with water — keep all tubes submerged",
                      ingredient=None, steps_total=2, steps_done=0,
                      target_g=0, poured_g=0, manual=[], finished_at=None)

        def run():
            try:
                for stage, (label, duration) in enumerate([
                    ("Flushing with water — keep all tubes submerged", seconds),
                    ("Purging — lift the tubes out of the water now", purge_seconds),
                ]):
                    self._set(step=label, steps_done=stage)
                    for line in range(1, self.pumps.count + 1):
                        self.pumps.on(line)
                    end = time.time() + duration
                    while time.time() < end:
                        if self._abort.is_set():
                            raise PourAborted("Clean stopped.")
                        time.sleep(0.05)
                    self.pumps.all_off()
                    if stage == 0:
                        time.sleep(3)  # gives you a moment to lift the tubes
                self._set(step="Clean finished", steps_done=2, finished_at=time.time())
            except PourAborted as e:
                self._set(step="Stopped", error=str(e))
            finally:
                self.pumps.all_off()
                self._set(busy=False)

        threading.Thread(target=run, daemon=True).start()
