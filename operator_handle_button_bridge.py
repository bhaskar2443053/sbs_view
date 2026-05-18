#!/usr/bin/env python3
"""
Bridge ROS operator_handle button topics into local toggle-state files.

This lets ROS button topics and the JX11 mapper trigger the same viewer actions.
"""

import argparse
import json
from pathlib import Path
import threading
import time
from typing import Optional

import roslibpy
from roslibpy.comm.comm_autobahn import AutobahnRosBridgeClientFactory


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DISPLAY_MODE_TOGGLE_STATE_FILE = SCRIPT_DIR / "display_mode_toggle.json"
DEFAULT_CLARIUS_TOGGLE_STATE_FILE = SCRIPT_DIR / "clarius_overlay_toggle.json"
DEFAULT_CLARIUS_CAPTURE_STATE_FILE = SCRIPT_DIR / "clarius_capture_request.json"
DEFAULT_OPERATOR_BUTTON_4_PLACEHOLDER_STATE_FILE = SCRIPT_DIR / "operator_button_4_placeholder.json"


def write_toggle_state_file(path_text: str | Path, source: str, button_name: str, topic: str) -> None:
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "toggle_id": time.time_ns(),
        "updated_at": time.time(),
        "source": source,
        "button": button_name,
        "topic": topic,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered in {"1", "true", "pressed", "down", "on", "active", "yes"}
    return bool(value)


def _message_is_pressed(msg: dict) -> bool:
    if not isinstance(msg, dict):
        return _coerce_bool(msg)

    for key in ("data", "pressed", "state", "value", "button", "active", "status"):
        if key in msg:
            return _coerce_bool(msg[key])

    if len(msg) == 1:
        only_value = next(iter(msg.values()))
        return _coerce_bool(only_value)

    return any(_coerce_bool(value) for value in msg.values())


class ButtonBinding:
    def __init__(
        self,
        name: str,
        topic: str,
        state_file: str,
        notes: str = "",
        debounce_sec: Optional[float] = None,
    ) -> None:
        self.name = name
        self.topic = topic
        self.state_file = state_file
        self.notes = notes
        self.debounce_sec = debounce_sec


class OperatorHandleButtonBridge:
    def __init__(
        self,
        host: str,
        port: int,
        reconnect_delay: float,
        resolve_interval: float,
        trigger_debounce: float,
        stale_press_reset: float,
        bindings: list[ButtonBinding],
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect_delay = max(0.5, reconnect_delay)
        self._resolve_interval = max(1.0, resolve_interval)
        self._trigger_debounce = max(0.0, trigger_debounce)
        self._stale_press_reset = max(0.05, stale_press_reset)
        self._bindings = bindings
        self._ros: Optional[roslibpy.Ros] = None
        self._listeners: dict[str, roslibpy.Topic] = {}
        self._last_pressed: dict[str, bool] = {binding.name: False for binding in bindings}
        self._last_state_change_at: dict[str, float] = {binding.name: 0.0 for binding in bindings}
        self._last_trigger_at: dict[str, float] = {binding.name: 0.0 for binding in bindings}
        self._resolved_types: dict[str, str] = {}
        self._waiting_logged: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._resolver_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        AutobahnRosBridgeClientFactory.set_initial_delay(self._reconnect_delay)
        AutobahnRosBridgeClientFactory.set_max_delay(self._reconnect_delay)
        AutobahnRosBridgeClientFactory.set_max_retries(None)

        self._ros = roslibpy.Ros(host=self._host, port=self._port)
        self._ros.on_ready(self._on_ready, run_in_thread=True)
        self._ros.on("close", self._on_close)

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._resolver_thread = threading.Thread(target=self._resolve_loop, daemon=True)
        self._resolver_thread.start()

    def stop(self) -> None:
        self._running = False
        self._clear_listeners()

        ros = self._ros
        self._ros = None
        if ros is not None:
            try:
                ros.terminate()
            except Exception:
                pass

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._resolver_thread is not None:
            self._resolver_thread.join(timeout=2.0)
            self._resolver_thread = None

    def _run(self) -> None:
        ros = self._ros
        if ros is None:
            return
        print(f"[Buttons] Connecting to ROS bridge {self._host}:{self._port}")
        try:
            ros.run_forever()
        except Exception as exc:
            if self._running:
                print(f"[Buttons] Receiver loop error: {exc}")

    def _on_ready(self) -> None:
        print("[Buttons] ROS bridge connected")

    def _on_close(self, *_args) -> None:
        self._clear_listeners()
        print(f"[Buttons] ROS bridge disconnected; retrying every {self._reconnect_delay:.1f}s")

    def _clear_listeners(self) -> None:
        for name, listener in list(self._listeners.items()):
            try:
                listener.unsubscribe()
            except Exception:
                pass
            self._listeners.pop(name, None)
        self._resolved_types.clear()
        self._waiting_logged.clear()
        for binding in self._bindings:
            self._last_pressed[binding.name] = False
            self._last_state_change_at[binding.name] = 0.0

    def _resolve_loop(self) -> None:
        while self._running:
            time.sleep(self._resolve_interval)
            ros = self._ros
            if ros is None or not ros.is_connected:
                continue
            for binding in self._bindings:
                if binding.name in self._listeners:
                    continue
                self._try_subscribe(binding)

    def _try_subscribe(self, binding: ButtonBinding) -> None:
        ros = self._ros
        if ros is None:
            return
        try:
            topic_type = ros.get_topic_type(binding.topic)
        except Exception as exc:
            if binding.name not in self._waiting_logged:
                print(f"[Buttons] Waiting for {binding.topic}: {exc}")
                self._waiting_logged.add(binding.name)
            return

        if not topic_type:
            if binding.name not in self._waiting_logged:
                print(f"[Buttons] Waiting for active topic type on {binding.topic}")
                self._waiting_logged.add(binding.name)
            return

        listener = roslibpy.Topic(
            ros,
            binding.topic,
            topic_type,
            queue_length=1,
            reconnect_on_close=True,
        )
        listener.subscribe(lambda msg, b=binding: self._on_message(b, msg))
        self._listeners[binding.name] = listener
        self._resolved_types[binding.name] = topic_type
        self._waiting_logged.discard(binding.name)
        print(f"[Buttons] Subscribed {binding.topic} ({topic_type}) -> {binding.state_file}")

    def _on_message(self, binding: ButtonBinding, msg: dict) -> None:
        now = time.monotonic()
        pressed = _message_is_pressed(msg)
        was_pressed = self._last_pressed.get(binding.name, False)
        last_state_change_at = self._last_state_change_at.get(binding.name, 0.0)
        if was_pressed and (now - last_state_change_at) >= self._stale_press_reset:
            was_pressed = False
            self._last_pressed[binding.name] = False

        if pressed != self._last_pressed.get(binding.name, False):
            self._last_pressed[binding.name] = pressed
            self._last_state_change_at[binding.name] = now

        if not pressed:
            return

        debounce_sec = binding.debounce_sec if binding.debounce_sec is not None else self._trigger_debounce
        last_trigger_at = self._last_trigger_at.get(binding.name, 0.0)
        if (now - last_trigger_at) < debounce_sec:
            return
        if was_pressed:
            return

        self._last_trigger_at[binding.name] = now
        write_toggle_state_file(
            binding.state_file,
            source="operator_handle_button_bridge",
            button_name=binding.name,
            topic=binding.topic,
        )
        if binding.notes:
            print(f"[Buttons] {binding.name} triggered: {binding.notes}")
        else:
            print(f"[Buttons] {binding.name} triggered")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge ROS operator_handle button topics into local toggle-state files."
    )
    parser.add_argument("--ros-host", default="192.168.6.1", help="ROS bridge host.")
    parser.add_argument("--ros-port", type=int, default=9090, help="ROS bridge port.")
    parser.add_argument("--reconnect-delay", type=float, default=5.0, help="Seconds between rosbridge reconnect attempts.")
    parser.add_argument("--resolve-interval", type=float, default=5.0, help="Seconds between unresolved topic-type checks.")
    parser.add_argument(
        "--trigger-debounce",
        type=float,
        default=1.0,
        help="Minimum seconds between accepted button press triggers on the same topic.",
    )
    parser.add_argument(
        "--stale-press-reset",
        type=float,
        default=0.5,
        help="Seconds after which a missing button-release is treated as released.",
    )
    parser.add_argument("--button-1-topic", default="/operator_handle/button_1", help="ROS topic for operator button 1.")
    parser.add_argument("--button-2-topic", default="/operator_handle/button_2", help="ROS topic for operator button 2.")
    parser.add_argument("--button-3-topic", default="/operator_handle/button_3", help="ROS topic for operator button 3.")
    parser.add_argument("--button-4-topic", default="/operator_handle/button_4", help="ROS topic for operator button 4.")
    parser.add_argument(
        "--button-1-file",
        default=str(DEFAULT_DISPLAY_MODE_TOGGLE_STATE_FILE),
        help="State file written when operator button 1 is pressed.",
    )
    parser.add_argument(
        "--button-2-file",
        default=str(DEFAULT_CLARIUS_TOGGLE_STATE_FILE),
        help="State file written when operator button 2 is pressed.",
    )
    parser.add_argument(
        "--button-3-file",
        default=str(DEFAULT_CLARIUS_CAPTURE_STATE_FILE),
        help="State file written when operator button 3 is pressed.",
    )
    parser.add_argument(
        "--button-4-file",
        default=str(DEFAULT_OPERATOR_BUTTON_4_PLACEHOLDER_STATE_FILE),
        help="State file written when operator button 4 is pressed.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bindings = [
        ButtonBinding("button_1", args.button_1_topic, args.button_1_file, notes="2D/3D display toggle", debounce_sec=1.5),
        ButtonBinding("button_2", args.button_2_topic, args.button_2_file, notes="Clarius overlay toggle", debounce_sec=1.5),
        ButtonBinding("button_3", args.button_3_topic, args.button_3_file, notes="Clarius snapshot", debounce_sec=0.75),
        ButtonBinding("button_4", args.button_4_topic, args.button_4_file, notes="Placeholder action", debounce_sec=1.0),
    ]

    bridge = OperatorHandleButtonBridge(
        host=args.ros_host,
        port=args.ros_port,
        reconnect_delay=args.reconnect_delay,
        resolve_interval=args.resolve_interval,
        trigger_debounce=args.trigger_debounce,
        stale_press_reset=args.stale_press_reset,
        bindings=bindings,
    )
    bridge.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
