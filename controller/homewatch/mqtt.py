"""Home Assistant through MQTT discovery: homeWatch appears in Home Assistant as one device with
- a status sensor (OK / WARNING / ERROR) and the number of problems and of things to do,
- a "problem" binary sensor per module,
- an event entity that fires when a problem starts and when it is back to OK (with the likely cause),
- a Check now button.
Everything goes through the MQTT broker Home Assistant already uses (e.g. the Mosquitto add-on); homeWatch only
needs a broker user. States are retained, so Home Assistant has them after its own restart; when homeWatch stops,
its last will marks the entities unavailable."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time

log = logging.getLogger("homewatch.mqtt")

SETTINGS = "mqtt"
SECRET = "mqtt.password"
DEFAULTS = {"enabled": False, "host": "", "port": 1883, "username": "", "tls": False, "prefix": "homeassistant"}


def load(db) -> dict:
    return DEFAULTS | (db.get(SETTINGS) or {})


class Bridge:
    def __init__(self, rt):
        self.rt = rt
        self.client = None
        self.connected = False
        self.error = ""
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()
        self.announced = set()

    # ---- topics ---------------------------------------------------------------------------------------------
    @property
    def base(self) -> str:
        return f"homewatch/{self.rt.cfg.site}"

    @property
    def uid(self) -> str:
        return f"homewatch_{self.rt.cfg.site}"

    def device(self) -> dict:
        from . import __version__
        d = {"identifiers": [self.uid], "name": self.rt.db.get("home.name") and f"homeWatch {self.rt.db.get('home.name')}"
             or "homeWatch", "manufacturer": "homeWatch", "model": "Home monitor", "sw_version": __version__}
        if (url := self.rt.db.get("home.url")):
            d["configuration_url"] = url
        return d

    def discovery(self, modules: list[tuple[str, str]]) -> list[tuple[str, dict]]:
        """(topic, config) for every entity; modules = [(id, name)] of the enabled ones."""
        s, prefix = self.base, load(self.rt.db)["prefix"]
        common = {"availability_topic": f"{s}/availability", "device": self.device()}
        out = [
            (f"{prefix}/sensor/{self.uid}/status/config", common | {
                "name": "Status", "unique_id": f"{self.uid}_status", "state_topic": f"{s}/state",
                "value_template": "{{ value_json.level_word }}", "icon": "mdi:shield-home",
                "json_attributes_topic": f"{s}/state", "json_attributes_template":
                    "{{ {'errors': value_json.errors, 'warnings': value_json.warnings, 'last_run': value_json.last_run} | tojson }}"}),
            (f"{prefix}/sensor/{self.uid}/problems/config", common | {
                "name": "Problems", "unique_id": f"{self.uid}_problems", "state_topic": f"{s}/state",
                "value_template": "{{ value_json.open_issues }}", "state_class": "measurement", "icon": "mdi:alert"}),
            (f"{prefix}/sensor/{self.uid}/todos/config", common | {
                "name": "To do", "unique_id": f"{self.uid}_todos", "state_topic": f"{s}/state",
                "value_template": "{{ value_json.todos }}", "icon": "mdi:update"}),
            (f"{prefix}/event/{self.uid}/alerts/config", common | {
                "name": "Alerts", "unique_id": f"{self.uid}_alerts", "state_topic": f"{s}/event",
                "event_types": ["problem", "back_to_ok"], "icon": "mdi:bell-alert"}),
            (f"{prefix}/button/{self.uid}/check_now/config", common | {
                "name": "Check now", "unique_id": f"{self.uid}_check_now", "command_topic": f"{s}/command",
                "payload_press": "check_now", "icon": "mdi:refresh"}),
        ]
        for mid, name in modules:
            out.append((f"{prefix}/binary_sensor/{self.uid}/{mid}/config", common | {
                "name": name, "unique_id": f"{self.uid}_{mid}", "device_class": "problem", "state_topic": f"{s}/state",
                "value_template": "{{ 'ON' if (value_json.module_levels." + mid + " | default(0)) > 0 else 'OFF' }}"}))
        return out

    # ---- connection -----------------------------------------------------------------------------------------
    def apply(self) -> None:
        """(Re)connect with the saved settings, or disconnect when switched off."""
        self.stop()
        s = load(self.rt.db)
        if not s["enabled"] or not s["host"]:
            self.error = ""
            return
        import paho.mqtt.client as mqtt

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.uid}_{int(time.time())}")
        if s["username"]:
            c.username_pw_set(s["username"], self.rt.secrets.get(SECRET) or "")
        if s["tls"]:
            c.tls_set(cert_reqs=ssl.CERT_NONE)
            c.tls_insecure_set(True)
        c.will_set(f"{self.base}/availability", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(5, 120)
        self.client = c
        try:
            c.connect_async(s["host"], int(s["port"]), keepalive=60)
            c.loop_start()
        except Exception as e:  # noqa: BLE001 - a wrong address: shown on the System page
            self.error = str(e)[:200]

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.publish(f"{self.base}/availability", "offline", retain=True)
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:  # noqa: BLE001
                pass
        self.client, self.connected, self.announced = None, False, set()

    def _on_connect(self, client, userdata, flags, reason, properties=None):
        if getattr(reason, "is_failure", False):
            self.connected, self.error = False, f"the broker refused: {reason}"
            return
        self.connected, self.error, self.announced = True, "", set()
        client.subscribe(f"{self.base}/command")
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish_state()

    def _on_disconnect(self, client, userdata, flags, reason, properties=None):
        self.connected = False
        if reason and getattr(reason, "is_failure", True) and str(reason) != "Success":
            self.error = f"disconnected: {reason}"

    def _on_message(self, client, userdata, msg):
        if msg.topic == f"{self.base}/command" and msg.payload == b"check_now" and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.rt.check_now(), self.loop)

    # ---- publishing -----------------------------------------------------------------------------------------
    def publish_state(self) -> None:
        if not (self.client and self.connected):
            return
        from . import api
        try:
            st = api.status(self.rt)
        except Exception:  # noqa: BLE001 - before the first run
            return
        mods = [(m["id"], m["name"]) for m in st["modules"]]
        if {m for m, _ in mods} != self.announced:
            for topic, cfg in self.discovery(mods):
                self.client.publish(topic, json.dumps(cfg), retain=True)
            self.announced = {m for m, _ in mods}
        st["module_levels"] = {m["id"]: m["level"] for m in st["modules"]}
        self.client.publish(f"{self.base}/state", json.dumps(st), retain=True)

    def publish_events(self, events) -> None:
        """Problems starting and coming back, as Home Assistant events (for automations)."""
        if not (self.client and self.connected):
            return
        for e in events:
            if e.kind in ("open", "worse"):
                kind = "problem"
            elif e.kind in ("resolve", "close"):
                kind = "back_to_ok"
            else:
                continue
            self.client.publish(f"{self.base}/event", json.dumps({
                "event_type": kind, "check": e.key, "name": e.name, "level": e.level, "detail": e.detail,
                "cause": e.cause}))
