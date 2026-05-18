#!/usr/bin/env python3
"""
Local force bridge server.

This process owns the ROS bridge connection and exposes the latest force value
over a small local HTTP endpoint for sbs_cast_view.py.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import threading
import time
from typing import Optional

import roslibpy
from roslibpy.comm.comm_autobahn import AutobahnRosBridgeClientFactory


class ForceState:
    def __init__(self, ros_host: str, ros_port: int, ros_topic: str, stale_after_s: float) -> None:
        self.ros_host = ros_host
        self.ros_port = ros_port
        self.ros_topic = ros_topic
        self.stale_after_s = stale_after_s
        self._lock = threading.Lock()
        self.force_kg = 0.0
        self.connected = False
        self.status = "starting"
        self.last_error = ""
        self.last_message_monotonic = 0.0
        self.last_update_unix = 0.0

    def mark_connecting(self) -> None:
        with self._lock:
            self.force_kg = 0.0
            self.connected = False
            self.status = "connecting"
            self.last_error = ""

    def mark_waiting_for_samples(self) -> None:
        with self._lock:
            self.force_kg = 0.0
            self.connected = False
            self.status = "connected_waiting"
            self.last_error = ""

    def update_force(self, value_kg: float) -> None:
        now_mono = time.monotonic()
        now_unix = time.time()
        with self._lock:
            self.force_kg = max(0.0, float(value_kg))
            self.connected = True
            self.status = "connected"
            self.last_error = ""
            self.last_message_monotonic = now_mono
            self.last_update_unix = now_unix

    def mark_disconnected(self, reason: str) -> None:
        with self._lock:
            self.force_kg = 0.0
            self.connected = False
            self.status = "disconnected"
            self.last_error = reason

    def snapshot(self) -> dict:
        now_mono = time.monotonic()
        with self._lock:
            force_kg = self.force_kg
            connected = self.connected
            status = self.status
            last_error = self.last_error
            last_message_monotonic = self.last_message_monotonic
            last_update_unix = self.last_update_unix

        sample_age_s: Optional[float] = None
        if last_message_monotonic > 0.0:
            sample_age_s = max(0.0, now_mono - last_message_monotonic)

        stale = sample_age_s is not None and sample_age_s > self.stale_after_s
        if stale:
            force_kg = 0.0
            connected = False
            if status == "connected":
                status = "stale"
                last_error = f"no force samples for {sample_age_s:.1f}s"

        return {
            "force_kg": round(force_kg, 6),
            "connected": connected,
            "status": status,
            "sample_age_s": sample_age_s,
            "updated_at_unix": last_update_unix,
            "ros_host": self.ros_host,
            "ros_port": self.ros_port,
            "ros_topic": self.ros_topic,
            "error": last_error,
        }


def _extract_force_kg(msg: dict) -> float:
    wrench = msg.get("wrench", {})
    force = wrench.get("force", {})
    fx = float(force.get("x", 0.0))
    fy = float(force.get("y", 0.0))
    fz = float(force.get("z", 0.0))
    return math.sqrt((fx * fx) + (fy * fy) + (fz * fz)) / 9.8


class ForceBridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "ForceBridge/1.0"

    def do_GET(self) -> None:
        if self.path != self.server.listen_path:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return

        payload = json.dumps(self.server.force_state.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


class ForceBridgeHttpServer(ThreadingHTTPServer):
    def __init__(self, server_address, request_handler_class, force_state: ForceState, listen_path: str) -> None:
        super().__init__(server_address, request_handler_class)
        self.force_state = force_state
        self.listen_path = listen_path


class RosForceSubscriber:
    def __init__(self, args: argparse.Namespace, state: ForceState) -> None:
        self._args = args
        self._state = state
        self._ros: Optional[roslibpy.Ros] = None
        self._topic: Optional[roslibpy.Topic] = None
        self._thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._seen_message = False

    def start(self) -> None:
        AutobahnRosBridgeClientFactory.set_initial_delay(self._args.reconnect_delay)
        AutobahnRosBridgeClientFactory.set_max_delay(self._args.reconnect_delay)
        AutobahnRosBridgeClientFactory.set_max_retries(None)

        self._state.mark_connecting()
        self._ros = roslibpy.Ros(host=self._args.ros_host, port=self._args.ros_port)
        self._ros.on_ready(self._on_ready, run_in_thread=True)
        self._ros.on("close", self._on_close)
        self._topic = roslibpy.Topic(
            self._ros,
            self._args.ros_topic,
            "geometry_msgs/WrenchStamped",
            queue_length=1,
            reconnect_on_close=True,
        )
        self._topic.subscribe(self._on_message)

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

    def stop(self) -> None:
        self._running = False

        if self._topic is not None:
            try:
                self._topic.unsubscribe()
            except Exception:
                pass
            self._topic = None

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
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

    def _run(self) -> None:
        ros = self._ros
        if ros is None:
            return
        print(
            f"[ForceBridge] Connecting to {self._args.ros_host}:{self._args.ros_port} "
            f"topic {self._args.ros_topic}"
        )
        try:
            ros.run_forever()
        except Exception as exc:
            if self._running:
                self._state.mark_disconnected(str(exc))
                print(f"[ForceBridge] Receiver loop error: {exc}")

    def _monitor(self) -> None:
        while self._running:
            time.sleep(0.5)
            ros = self._ros
            if ros is None:
                continue
            if ros.is_connected:
                if not self._seen_message:
                    self._state.mark_waiting_for_samples()
            else:
                self._state.mark_disconnected("rosbridge disconnected")
                self._seen_message = False

    def _on_ready(self) -> None:
        self._seen_message = False
        self._state.mark_waiting_for_samples()
        print("[ForceBridge] ROS bridge connected; waiting for force samples")

    def _on_close(self, *_args) -> None:
        self._seen_message = False
        self._state.mark_disconnected("rosbridge disconnected")
        print(f"[ForceBridge] Disconnected; reconnecting in {self._args.reconnect_delay:.1f}s")

    def _on_message(self, msg: dict) -> None:
        self._seen_message = True
        self._state.update_force(_extract_force_kg(msg))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to a ROS bridge force topic and expose the latest kg reading over local HTTP."
    )
    parser.add_argument("--ros-host", default="192.168.6.1", help="ROS bridge host.")
    parser.add_argument("--ros-port", type=int, default=9090, help="ROS bridge port.")
    parser.add_argument(
        "--ros-topic",
        default="/protect/follower_state_controller/F_ext",
        help="ROS topic carrying geometry_msgs/WrenchStamped force data.",
    )
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local HTTP bind host.")
    parser.add_argument("--listen-port", type=int, default=8765, help="Local HTTP bind port.")
    parser.add_argument("--listen-path", default="/force", help="Local HTTP path to serve force JSON.")
    parser.add_argument("--reconnect-delay", type=float, default=1.0, help="Seconds between rosbridge reconnect attempts.")
    parser.add_argument(
        "--stale-after",
        type=float,
        default=0.75,
        help="Serve 0 kg if no fresh force sample is received for this many seconds.",
    )
    args = parser.parse_args()
    if not args.listen_path.startswith("/"):
        args.listen_path = "/" + args.listen_path
    return args


def main() -> None:
    args = parse_args()
    state = ForceState(args.ros_host, args.ros_port, args.ros_topic, args.stale_after)
    subscriber = RosForceSubscriber(args, state)
    server = ForceBridgeHttpServer(
        (args.listen_host, args.listen_port),
        ForceBridgeRequestHandler,
        state,
        args.listen_path,
    )

    print(
        f"[ForceBridge] Serving http://{args.listen_host}:{args.listen_port}{args.listen_path} "
        f"and forwarding {args.ros_topic} from {args.ros_host}:{args.ros_port}"
    )
    subscriber.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        server.server_close()
        subscriber.stop()


if __name__ == "__main__":
    main()
