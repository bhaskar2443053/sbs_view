#!/usr/bin/env python3
"""
Render a live ZED side-by-side view in a full-screen OpenCV window,
with a direct Clarius ultrasound picture-in-picture overlay via the Cast API
and an optional force gauge fed through rosbridge.
"""

import argparse
import ctypes
from contextlib import contextmanager
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import socket
import concurrent.futures
import wave
from typing import Callable, Dict, Iterator, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

import cv2
import numpy as np
import pyzed.sl as sl

# Add the camera_streaming directory to path to find pyclariuscast
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

try:
    import pyclariuscast
    HAS_CAST = True
except ImportError:
    HAS_CAST = False

try:
    import roslibpy
    HAS_ROSLIBPY = True
except ImportError:
    HAS_ROSLIBPY = False

if HAS_ROSLIBPY:
    try:
        from roslibpy.comm.comm_autobahn import AutobahnRosBridgeClientFactory
        HAS_ROSLIBPY_AUTORECONNECT = True
    except ImportError:
        HAS_ROSLIBPY_AUTORECONNECT = False
else:
    HAS_ROSLIBPY_AUTORECONNECT = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VOICE_STATE_FILE = SCRIPT_DIR / "remote_voice_state.json"
DEFAULT_DISPLAY_MODE_TOGGLE_STATE_FILE = SCRIPT_DIR / "display_mode_toggle.json"
DEFAULT_CLARIUS_TOGGLE_STATE_FILE = SCRIPT_DIR / "clarius_overlay_toggle.json"
DEFAULT_CLARIUS_CAPTURE_STATE_FILE = SCRIPT_DIR / "clarius_capture_request.json"
DEFAULT_CLARIUS_FREEZE_STATE_FILE = SCRIPT_DIR / "clarius_freeze_request.json"
DEFAULT_CLARIUS_CONTRAST_UP_STATE_FILE = SCRIPT_DIR / "clarius_contrast_up_request.json"
DEFAULT_CLARIUS_CONTRAST_DOWN_STATE_FILE = SCRIPT_DIR / "clarius_contrast_down_request.json"
DEFAULT_OPERATOR_BUTTON_4_PLACEHOLDER_STATE_FILE = SCRIPT_DIR / "operator_button_4_placeholder.json"
DEFAULT_CLARIUS_CAPTURE_DIR = SCRIPT_DIR / "clarius_captures"
DEFAULT_FORCE_BRIDGE_URL = "http://127.0.0.1:8765/force"
CAPTURE_CONFIRMATION_TEXT = "CAPTURED"
CAPTURE_CONFIRMATION_SECONDS = 1.2
CLARIUS_RECONNECT_INTERVAL_S = 2.0
CLARIUS_INITIAL_FRAME_TIMEOUT_S = 8.0
CLARIUS_FRAME_STALE_WARN_S = 2.0
CLARIUS_FRAME_STALE_RECONNECT_S = 6.0
CLARIUS_OVERLAY_OFF_NOTICE_SECONDS = 3.0
FORCE_GAUGE_MAX_KG = 2.5
FORCE_ALERT_THRESHOLD_KG = 1.5
FORCE_ALERT_RELEASE_THRESHOLD_KG = 1.35
FORCE_ALERT_TONE_HZ = 1400
FORCE_ALERT_BEEP_MS = 80
FORCE_ALERT_MAX_INTERVAL_S = 1.0
FORCE_ALERT_MIN_INTERVAL_S = 0.1
FORCE_ALERT_HOLD_S = 0.35
FORCE_ENDPOINT_VALUE_HOLD_S = 0.35
FORCE_ALERT_RECOVERY_RETRY_S = 0.1
FORCE_DIRECT_RECONNECT_DELAY_S = 2.0
FORCE_LOOP_SLEEP_S = 0.001
FORCE_DISPLAY_ATTACK_S = 0.04
FORCE_DISPLAY_RELEASE_S = 0.09


# Map CLI resolution names to ZED SDK enums.
RESOLUTION_MAP: Dict[str, sl.RESOLUTION] = {
    "HD720": sl.RESOLUTION.HD720,
    "HD1080": sl.RESOLUTION.HD1080,
    "HD2K": sl.RESOLUTION.HD2K,
    "HD4K": sl.RESOLUTION.HD4K,
}


if sys.platform == "win32":
    from ctypes import wintypes
    import winsound

    try:
        _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
        _shcore = ctypes.windll.shcore
        _user32 = ctypes.windll.user32

        def _set_dpi_awareness() -> None:
            try:
                if _user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                    return
            except Exception: pass
            try:
                if _user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE):
                    return
            except Exception: pass
            try:
                _shcore.SetProcessDpiAwareness(2)
                return
            except Exception: pass
            try:
                _user32.SetProcessDPIAware()
            except Exception: pass

    except Exception:
        def _set_dpi_awareness() -> None:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception: pass

    class _Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class _Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", _Rect),
            ("rcWork", _Rect),
            ("dwFlags", wintypes.DWORD),
        ]

    _GWL_STYLE = -16
    _GWL_EXSTYLE = -20
    _WS_CAPTION = 0x00C00000
    _WS_THICKFRAME = 0x00040000
    _WS_MINIMIZE = 0x20000000
    _WS_MAXIMIZEBOX = 0x00010000
    _WS_SYSMENU = 0x00080000
    _WS_POPUP = 0x80000000
    _WS_EX_TOPMOST = 0x00000008
    _SWP_SHOWWINDOW = 0x0040
    _SWP_FRAMECHANGED = 0x0020
    _SWP_NOOWNERZORDER = 0x0200
    _HWND_TOPMOST = -1
    _MONITOR_DEFAULTTONEAREST = 2
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _VK_MENU = 0x12
    _VK_Q = 0x51
    _VK_W = 0x57

    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("type", ctypes.c_uint),
            ("u", _INPUTUNION),
        ]

    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _get_monitor_placement(index: int) -> Optional[Dict[str, int]]:
    """Return monitor geometry for the given index (Windows only)."""
    if sys.platform != "win32" or index < 0:
        return None

    placements: list[Dict[str, int]] = []
    user32 = ctypes.windll.user32

    def _callback(hmon, hdc, lprect, lparam) -> int:
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            placements.append({
                "left": info.rcMonitor.left,
                "top": info.rcMonitor.top,
                "width": info.rcMonitor.right - info.rcMonitor.left,
                "height": info.rcMonitor.bottom - info.rcMonitor.top,
            })
        return 1

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_Rect), wintypes.LPARAM)(_callback)
    user32.EnumDisplayMonitors(None, None, enum_proc, 0)

    if 0 <= index < len(placements):
        return placements[index]
    return None

def _list_monitors() -> list[Dict[str, int]]:
    """Enumerate monitors (Windows only)."""
    if sys.platform != "win32":
        return []
    placements: list[Dict[str, int]] = []
    user32 = ctypes.windll.user32

    def _callback(hmon, hdc, lprect, lparam) -> int:
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            placements.append({
                "left": info.rcMonitor.left,
                "top": info.rcMonitor.top,
                "width": info.rcMonitor.right - info.rcMonitor.left,
                "height": info.rcMonitor.bottom - info.rcMonitor.top,
            })
        return 1

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_Rect), wintypes.LPARAM)(_callback)
    user32.EnumDisplayMonitors(None, None, enum_proc, 0)
    return placements


def _get_force_alert_interval_s(force_kg: float) -> Optional[float]:
    """Return beep interval with beep rate linearly proportional to force."""
    if force_kg < FORCE_ALERT_THRESHOLD_KG:
        return None
    clamped_force_kg = min(
        max(force_kg, FORCE_ALERT_THRESHOLD_KG),
        FORCE_GAUGE_MAX_KG,
    )
    ratio = (clamped_force_kg - FORCE_ALERT_THRESHOLD_KG) / max(
        FORCE_GAUGE_MAX_KG - FORCE_ALERT_THRESHOLD_KG,
        1e-6,
    )
    min_rate_hz = 1.0 / max(FORCE_ALERT_MAX_INTERVAL_S, 1e-6)
    max_rate_hz = 1.0 / max(FORCE_ALERT_MIN_INTERVAL_S, 1e-6)
    beep_rate_hz = min_rate_hz + (ratio * (max_rate_hz - min_rate_hz))
    return 1.0 / max(beep_rate_hz, 1e-6)


class ForceAlertAudioPlayer:
    """Play the force alert tone through the normal audio device when possible."""

    def __init__(self) -> None:
        self._backend = "sleep"
        self._stream: Optional[sd.OutputStream] = None if HAS_SOUNDDEVICE else None
        self._tone_samples: Optional[np.ndarray] = None
        self._tone_duration_s = max(0.0, FORCE_ALERT_BEEP_MS / 1000.0)

    def start(self) -> None:
        if not HAS_SOUNDDEVICE:
            if sys.platform == "win32":
                self._backend = "winsound"
            return

        try:
            info = sd.query_devices(kind="output")
            channels = 2 if int(info["max_output_channels"]) >= 2 else 1
            sample_rate = int(round(float(info["default_samplerate"]))) or 48000
            self._stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="float32",
                blocksize=0,
                latency="low",
            )
            self._stream.start()
            self._tone_samples = self._build_tone(sample_rate, channels)
            self._backend = "sounddevice"
            print(f"[Force] Alert audio output: {_describe_audio_device(None)} via sounddevice")
        except Exception as exc:
            self._stream = None
            self._tone_samples = None
            if sys.platform == "win32":
                self._backend = "winsound"
                print(f"[Force] sounddevice alert output unavailable, falling back to winsound: {exc}")
            else:
                self._backend = "sleep"
                print(f"[Force] sounddevice alert output unavailable: {exc}")

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def play_beep(self) -> None:
        if self._backend == "sounddevice" and self._stream is not None and self._tone_samples is not None:
            self._stream.write(self._tone_samples)
            return
        if self._backend == "winsound":
            winsound.Beep(FORCE_ALERT_TONE_HZ, FORCE_ALERT_BEEP_MS)
            return
        time.sleep(self._tone_duration_s)

    def restart(self) -> None:
        self.stop()
        self.start()

    def _build_tone(self, sample_rate: int, channels: int) -> np.ndarray:
        sample_count = max(1, int(round(sample_rate * self._tone_duration_s)))
        t = np.arange(sample_count, dtype=np.float32) / float(sample_rate)
        envelope = np.hanning(sample_count).astype(np.float32)
        tone = (0.28 * np.sin((2.0 * np.pi * FORCE_ALERT_TONE_HZ) * t) * envelope).astype(np.float32)
        if channels == 1:
            return tone.reshape((-1, 1))
        return np.repeat(tone.reshape((-1, 1)), channels, axis=1)


def _run_force_alert_loop(
    is_running: Callable[[], bool],
    get_force_value: Callable[[], float],
) -> None:
    """Drive repetitive force alert beeps with low-latency force-based cadence."""
    player = ForceAlertAudioPlayer()
    player.start()
    gate_open = False
    hold_until_monotonic = 0.0
    next_beep_monotonic = time.monotonic()
    last_beep_started_monotonic = 0.0

    try:
        while is_running():
            now = time.monotonic()
            current_force_kg = max(0.0, float(get_force_value()))

            gate_was_open = gate_open
            if current_force_kg >= FORCE_ALERT_THRESHOLD_KG:
                gate_open = True
                hold_until_monotonic = now + FORCE_ALERT_HOLD_S
            elif current_force_kg <= FORCE_ALERT_RELEASE_THRESHOLD_KG and now >= hold_until_monotonic:
                gate_open = False

            if gate_open and not gate_was_open:
                next_beep_monotonic = now
                last_beep_started_monotonic = 0.0

            if not gate_open:
                next_beep_monotonic = now
                last_beep_started_monotonic = 0.0
                time.sleep(FORCE_LOOP_SLEEP_S)
                continue

            cadence_force_kg = max(current_force_kg, FORCE_ALERT_THRESHOLD_KG)
            interval_s = _get_force_alert_interval_s(cadence_force_kg)
            if interval_s is None:
                next_beep_monotonic = now
                time.sleep(FORCE_LOOP_SLEEP_S)
                continue

            if last_beep_started_monotonic > 0.0:
                faster_next_beep = last_beep_started_monotonic + interval_s
                if faster_next_beep < next_beep_monotonic:
                    next_beep_monotonic = faster_next_beep

            if now + 0.002 < next_beep_monotonic:
                time.sleep(min(FORCE_LOOP_SLEEP_S, max(0.0, next_beep_monotonic - now)))
                continue

            beep_started_monotonic = time.monotonic()
            try:
                player.play_beep()
            except Exception as exc:
                print(f"[Force] Alert audio playback error; restarting audio backend: {exc}")
                time.sleep(FORCE_ALERT_RECOVERY_RETRY_S)
                player.restart()
                next_beep_monotonic = time.monotonic() + FORCE_ALERT_RECOVERY_RETRY_S
                continue

            last_beep_started_monotonic = beep_started_monotonic
            next_beep_monotonic = beep_started_monotonic + interval_s
    finally:
        player.stop()


def _get_monitor_scale(placement: Optional[Dict[str, int]]) -> float:
    """Return DPI scale factor for a monitor (1.0 = 96 DPI)."""
    if sys.platform != "win32" or placement is None:
        return 1.0
    try:
        shcore = ctypes.windll.shcore
        user32 = ctypes.windll.user32
        pt = _Point(placement["left"], placement["top"])
        hmon = user32.MonitorFromPoint(pt, _MONITOR_DEFAULTTONEAREST)
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return float(dpi_x.value) / 96.0
    except Exception:
        pass
    return 1.0


def _configure_window_on_monitor(window_name: str, placement: Optional[Dict[str, int]], width: int, height: int) -> None:
    """Make the OpenCV window borderless/topmost and position it on a monitor."""
    if sys.platform != "win32" or placement is None:
        return

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, window_name)
    if not hwnd:
        return

    parent = user32.GetParent(hwnd)
    if parent:
        hwnd = parent

    style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
    style &= ~(_WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZE | _WS_MAXIMIZEBOX | _WS_SYSMENU)
    style |= _WS_POPUP
    user32.SetWindowLongW(hwnd, _GWL_STYLE, style)
    user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) | _WS_EX_TOPMOST)
    user32.SetWindowPos(
        hwnd, _HWND_TOPMOST, placement["left"], placement["top"], width, height,
        _SWP_SHOWWINDOW | _SWP_FRAMECHANGED | _SWP_NOOWNERZORDER,
    )


def _is_alt_w_pressed() -> bool:
    """Return True while Alt+W is held down (Windows only)."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    alt_down = bool(user32.GetAsyncKeyState(_VK_MENU) & 0x8000)
    w_down = bool(user32.GetAsyncKeyState(_VK_W) & 0x8000)
    return alt_down and w_down


def _is_alt_q_pressed() -> bool:
    """Return True while Alt+Q is held down (Windows only)."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    alt_down = bool(user32.GetAsyncKeyState(_VK_MENU) & 0x8000)
    q_down = bool(user32.GetAsyncKeyState(_VK_Q) & 0x8000)
    return alt_down and q_down


def _send_windows_hotkey(*virtual_keys: int) -> bool:
    if sys.platform != "win32" or not virtual_keys:
        return False
    inputs = []
    for vk in virtual_keys:
        inputs.append(_INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=int(vk))))
    for vk in reversed(virtual_keys):
        inputs.append(_INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=int(vk), dwFlags=_KEYEVENTF_KEYUP)))

    input_array = (_INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(input_array), input_array, ctypes.sizeof(_INPUT))
    return sent == len(input_array)


def _trigger_display_mode_toggle() -> bool:
    return _send_windows_hotkey(_VK_MENU, _VK_Q)


def _get_window_rect(window_name: str) -> Optional[Dict[str, int]]:
    """Return actual window rectangle (Windows only)."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, window_name)
    if not hwnd:
        return None
    rect = _Rect()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {
            "left": rect.left, "top": rect.top,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top,
        }
    return None


def _coerce_device_id(value: Optional[str]) -> Optional[int | str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _list_audio_devices() -> None:
    if not HAS_SOUNDDEVICE:
        print("Audio device listing requires sounddevice.")
        return
    default_input, default_output = sd.default.device
    for index, info in enumerate(sd.query_devices()):
        flags = []
        if info["max_input_channels"] > 0:
            flags.append("input")
        if info["max_output_channels"] > 0:
            flags.append("output")
        if index == default_input:
            flags.append("default-in")
        if index == default_output:
            flags.append("default-out")
        label = ", ".join(flags) if flags else "unused"
        print(
            f"[{index}] {info['name']} | {label} | "
            f"in={info['max_input_channels']} out={info['max_output_channels']}"
        )


def _describe_audio_device(device: Optional[int | str]) -> str:
    if device is None:
        return "default"
    if not HAS_SOUNDDEVICE:
        return str(device)
    try:
        info = sd.query_devices(device)
        return f"{info['name']} [{device}]"
    except Exception:
        return str(device)


def _find_monitor_input_device(preferred: Optional[int | str]) -> tuple[int | str, dict]:
    if not HAS_SOUNDDEVICE:
        raise RuntimeError("sounddevice is not installed")

    if preferred is not None:
        info = sd.query_devices(preferred)
        if int(info["max_input_channels"]) <= 0:
            raise RuntimeError(
                f"Monitor source '{preferred}' is not an input device. "
                "Use a monitor input such as Stereo Mix."
            )
        return preferred, info

    candidates: list[tuple[int, int, dict]] = []
    for index, info in enumerate(sd.query_devices()):
        if int(info["max_input_channels"]) <= 0:
            continue
        name = str(info["name"])
        lowered = name.lower()
        score = 0
        if "stereo mix" in lowered:
            score += 200
        if "what u hear" in lowered or "what-you-hear" in lowered or "what you hear" in lowered:
            score += 180
        if "wave out mix" in lowered:
            score += 170
        if "monitor" in lowered:
            score += 130
        if "loopback" in lowered:
            score += 120
        if "mix" in lowered:
            score += 20
        if score > 0:
            candidates.append((score, index, info))

    if not candidates:
        raise RuntimeError(
            "No passive monitor input device was found. "
            "Use --voice-source input or select a monitor input explicitly."
        )

    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, index, info = candidates[0]
    return index, info


class RemoteVoiceStateReader:
    """Read the iPad-side speaking level published by the WebRTC server."""

    def __init__(self, state_path: str | os.PathLike[str], smoothing: float) -> None:
        self._state_path = Path(state_path)
        self._smoothing = max(0.0, min(smoothing, 0.995))
        self._level = 0.0
        self._history = np.zeros(64, dtype=np.float32)
        self._last_seen_mtime_ns: Optional[int] = None
        self._last_remote_update = 0.0
        self._last_local_tick = time.monotonic()

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def get_level(self) -> float:
        self._tick()
        return self._level

    def describe_source(self) -> str:
        return f"remote-state file: {self._state_path}"

    def get_visual_state(self, samples: int) -> tuple[float, np.ndarray]:
        self._tick()
        level = self._level
        history = self._history.copy()

        if samples > 0 and samples != history.size:
            src = np.linspace(0.0, 1.0, history.size, dtype=np.float32)
            dst = np.linspace(0.0, 1.0, samples, dtype=np.float32)
            history = np.interp(dst, src, history).astype(np.float32)

        return level, history

    def _tick(self) -> None:
        now = time.monotonic()
        if (now - self._last_local_tick) < 0.03:
            return
        self._last_local_tick = now

        target = self._read_remote_level()
        if target >= self._level:
            self._level = (self._smoothing * self._level) + ((1.0 - self._smoothing) * target)
        else:
            decay = min(0.94, max(self._smoothing, 0.78))
            self._level = (decay * self._level) + ((1.0 - decay) * target)

        self._level = max(0.0, min(self._level, 1.0))
        self._history[:-1] = self._history[1:]
        self._history[-1] = self._level

    def _read_remote_level(self) -> float:
        try:
            stat = self._state_path.stat()
        except OSError:
            self._last_seen_mtime_ns = None
            self._last_remote_update = 0.0
            return 0.0

        if self._last_seen_mtime_ns != stat.st_mtime_ns:
            self._last_seen_mtime_ns = stat.st_mtime_ns
            try:
                payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                return 0.0
            self._last_remote_update = float(payload.get("updated_at", 0.0))
            if not bool(payload.get("active", False)):
                return 0.0
            return max(0.0, min(float(payload.get("level", 0.0)), 1.0))

        if self._last_remote_update <= 0.0:
            return 0.0
        if (time.time() - self._last_remote_update) > 1.2:
            return 0.0
        return self._level


class StateFileEventReader:
    """Watch a small state file and emit one event per file update."""

    def __init__(self, state_path: str | os.PathLike[str]) -> None:
        self._state_path = Path(state_path)
        try:
            self._last_seen_mtime_ns: Optional[int] = self._state_path.stat().st_mtime_ns
        except OSError:
            self._last_seen_mtime_ns = None

    def poll_toggle(self) -> bool:
        try:
            stat = self._state_path.stat()
        except OSError:
            return False

        if self._last_seen_mtime_ns is None:
            self._last_seen_mtime_ns = stat.st_mtime_ns
            return True

        if stat.st_mtime_ns != self._last_seen_mtime_ns:
            self._last_seen_mtime_ns = stat.st_mtime_ns
            return True
        return False


class ComponentProfiler:
    """Collect and report rolling per-stage timings for the main render loop."""

    _ORDER = (
        "loop",
        "grab",
        "retrieve",
        "controls",
        "clarius",
        "force",
        "hud",
        "compose",
        "display",
        "input",
    )

    def __init__(self, enabled: bool, report_interval: float) -> None:
        self.enabled = enabled
        self.report_interval = max(0.25, float(report_interval))
        self._elapsed_by_name: dict[str, float] = {}
        self._count_by_name: dict[str, int] = {}
        self._last_report_at = time.perf_counter()
        self._overlay_lines: list[str] = []

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - start)

    def record(self, name: str, elapsed_sec: float) -> None:
        if not self.enabled:
            return
        self._elapsed_by_name[name] = self._elapsed_by_name.get(name, 0.0) + max(0.0, elapsed_sec)
        self._count_by_name[name] = self._count_by_name.get(name, 0) + 1

    def finish_iteration(self) -> list[str]:
        if not self.enabled:
            return self._overlay_lines

        now = time.perf_counter()
        if (now - self._last_report_at) < self.report_interval:
            return self._overlay_lines

        avgs_ms: dict[str, float] = {}
        for name, total in self._elapsed_by_name.items():
            count = max(1, self._count_by_name.get(name, 1))
            avgs_ms[name] = (total / count) * 1000.0

        loop_ms = avgs_ms.get("loop", 0.0)
        loop_fps = (1000.0 / loop_ms) if loop_ms > 1e-6 else 0.0

        self._overlay_lines = [
            f"Profile avg {self.report_interval:.2f}s window",
            (
                f"loop {loop_ms:.1f} ms ({loop_fps:.1f} FPS) | "
                f"grab {avgs_ms.get('grab', 0.0):.1f} | "
                f"retrieve {avgs_ms.get('retrieve', 0.0):.1f} | "
                f"controls {avgs_ms.get('controls', 0.0):.1f}"
            ),
            (
                f"clarius {avgs_ms.get('clarius', 0.0):.1f} | "
                f"force {avgs_ms.get('force', 0.0):.1f} | "
                f"hud {avgs_ms.get('hud', 0.0):.1f}"
            ),
            (
                f"compose {avgs_ms.get('compose', 0.0):.1f} | "
                f"display {avgs_ms.get('display', 0.0):.1f} | "
                f"input {avgs_ms.get('input', 0.0):.1f}"
            ),
        ]

        summary_parts = []
        for name in self._ORDER:
            if name not in avgs_ms:
                continue
            if name == "loop":
                summary_parts.append(f"{name}={avgs_ms[name]:.1f}ms/{loop_fps:.1f}fps")
            else:
                summary_parts.append(f"{name}={avgs_ms[name]:.1f}ms")
        if summary_parts:
            print("[Profile] " + " | ".join(summary_parts))

        self._elapsed_by_name.clear()
        self._count_by_name.clear()
        self._last_report_at = now
        return self._overlay_lines

    def get_overlay_lines(self) -> list[str]:
        return self._overlay_lines if self.enabled else []


def _save_clarius_snapshot(
    frame: np.ndarray,
    output_dir: Path,
    prefix: str = "clarius",
    timestamp: Optional[str] = None,
) -> Optional[Path]:
    """Save a Clarius frame to disk and return the written path."""
    if frame is None or frame.size == 0:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_timestamp = timestamp or _make_capture_timestamp()
    path = output_dir / f"{prefix}_{capture_timestamp}.png"
    if cv2.imwrite(str(path), frame):
        return path
    return None


def _make_capture_timestamp() -> str:
    """Return one timestamp string that can be shared by paired captures."""
    wall_time = time.time()
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(wall_time))
    millis = int((wall_time % 1.0) * 1000)
    return f"{timestamp}_{millis:03d}"


def _frame_to_bgr(frame: np.ndarray) -> Optional[np.ndarray]:
    if frame is None or frame.size == 0:
        return None
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim != 3:
        return None
    channels = frame.shape[2]
    if channels == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    if channels == 3:
        return np.ascontiguousarray(frame)
    if channels == 1:
        return cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(frame[:, :, :3])


def _save_zed_sbs_snapshot(
    left_frame: np.ndarray,
    right_frame: np.ndarray,
    output_dir: Path,
    timestamp: str,
    prefix: str = "zed_sbs",
) -> Optional[Path]:
    """Save the current ZED left/right feed as one side-by-side image."""
    left_bgr = _frame_to_bgr(left_frame)
    right_bgr = _frame_to_bgr(right_frame)
    if left_bgr is None or right_bgr is None:
        return None
    if left_bgr.shape[:2] != right_bgr.shape[:2]:
        right_bgr = cv2.resize(
            right_bgr,
            (left_bgr.shape[1], left_bgr.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    sbs_frame = np.ascontiguousarray(np.concatenate((left_bgr, right_bgr), axis=1))
    path = output_dir / f"{prefix}_{timestamp}.png"
    if cv2.imwrite(str(path), sbs_frame):
        return path
    return None


_CAMERA_CLICK_WAV_BYTES: Optional[bytes] = None


def _build_camera_click_wav() -> bytes:
    sample_rate = 44100
    duration_s = 0.16
    sample_count = int(round(sample_rate * duration_s))
    audio = np.zeros(sample_count, dtype=np.float32)
    rng = np.random.default_rng(2443)

    def add_click(start_s: float, click_s: float, tone_hz: float, gain: float) -> None:
        start = int(round(start_s * sample_rate))
        count = max(1, int(round(click_s * sample_rate)))
        end = min(sample_count, start + count)
        if end <= start:
            return
        idx = np.arange(end - start, dtype=np.float32)
        envelope = np.exp(-idx / max(1.0, count * 0.28)).astype(np.float32)
        tone = np.sin((2.0 * np.pi * tone_hz / sample_rate) * idx).astype(np.float32)
        noise = rng.uniform(-1.0, 1.0, end - start).astype(np.float32)
        audio[start:end] += gain * envelope * ((0.58 * noise) + (0.42 * tone))

    add_click(0.000, 0.020, 3200.0, 0.85)
    add_click(0.048, 0.032, 1500.0, 0.58)
    add_click(0.095, 0.014, 4300.0, 0.28)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _get_camera_click_wav() -> bytes:
    global _CAMERA_CLICK_WAV_BYTES
    if _CAMERA_CLICK_WAV_BYTES is None:
        _CAMERA_CLICK_WAV_BYTES = _build_camera_click_wav()
    return _CAMERA_CLICK_WAV_BYTES


def _play_camera_click_sound() -> None:
    """Play a short non-blocking shutter-click sound on Windows."""
    if sys.platform != "win32":
        return

    def play() -> None:
        try:
            winsound.PlaySound(_get_camera_click_wav(), winsound.SND_MEMORY)
        except Exception:
            try:
                winsound.Beep(2600, 18)
                time.sleep(0.035)
                winsound.Beep(1200, 28)
            except Exception:
                pass

    threading.Thread(target=play, name="CameraClickSound", daemon=True).start()


def _apply_image_contrast(frame: np.ndarray, contrast: float) -> np.ndarray:
    """Apply a simple center-preserving contrast adjustment."""
    if frame is None or frame.size == 0:
        return frame
    if abs(contrast - 1.0) < 1e-3:
        return frame
    frame_f = frame.astype(np.float32)
    adjusted = ((frame_f - 127.5) * contrast) + 127.5
    return np.clip(adjusted, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
#  Clarius Cast API Receiver
# ---------------------------------------------------------------------------

class ClariusCastReceiver:
    """Connects to a Clarius ultrasound probe via Cast API in a background thread."""

    def __init__(self, target_ip: str, port: int, pip_size: tuple) -> None:
        self._requested_target_ip = target_ip
        self._target_ip = target_ip
        self._port = port
        self._pip_w, self._pip_h = pip_size
        self._lock = threading.Lock()
        self._front_frame: Optional[np.ndarray] = None
        self._connected = False
        self._frozen = False
        self._cast = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._connected_at_monotonic = 0.0
        self._last_frame_monotonic = 0.0
        self._last_status = "not started"
        
        # FPS Tracking
        self._frame_count = 0
        self._last_fps_time = time.time()
        self._current_fps = 0.0
        
        # Default keys dir, change this if necessary
        self._keys_dir = os.path.expanduser("~/")

    def _auto_discover_probe(self):
        """Scans the local network for an open Cast API port."""
        def check_port(ip):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                if s.connect_ex((ip, self._port)) == 0:
                    return ip
            except Exception:
                pass
            finally:
                s.close()
            return None

        local_ips = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ips.append(s.getsockname()[0])
            s.close()
        except Exception: pass
            
        try:
            _, _, ips = socket.gethostbyname_ex(socket.gethostname())
            local_ips.extend(ips)
        except Exception: pass

        local_ips = list(set(local_ips))
        if not local_ips:
            local_ips = ['192.168.1.1']

        subnets = set()
        for ip in local_ips:
            if not ip.startswith('127.'):
                parts = ip.split('.')
                if len(parts) == 4:
                    subnets.add(f"{parts[0]}.{parts[1]}.{parts[2]}")

        ips_to_scan = []
        for subnet in subnets:
            ips_to_scan.extend([f"{subnet}.{i}" for i in range(1, 255)])

        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as e:
            results = [ip for ip in e.map(check_port, ips_to_scan) if ip]
            
        if results:
            return results[0]
        return None

    # Cast Callbacks
    def _newProcessedImage(self, image, width, height, sz, micronsPerPixel, timestamp, angle, imu):
        bpp = sz // (width * height) if (width * height) > 0 else 4
        try:
            if bpp == 4:
                img_array = np.frombuffer(image, dtype=np.uint8).reshape((height, width, 4))
                bgr = cv2.cvtColor(img_array, cv2.COLOR_BGRA2BGR)
            else:
                img_array = np.frombuffer(image, dtype=np.uint8).reshape((height, width))
                bgr = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)

            with self._lock:
                self._front_frame = bgr.copy()
                self._frame_count += 1
                self._last_frame_monotonic = time.monotonic()
                if not self._connected:
                    self._connected = True
                    self._connected_at_monotonic = self._last_frame_monotonic
                self._last_status = "streaming"
                now = time.time()
                if now - self._last_fps_time >= 1.0:
                    self._current_fps = self._frame_count / (now - self._last_fps_time)
                    self._frame_count = 0
                    self._last_fps_time = now

        except Exception as e:
            print(f"[Clarius] Failed to process image: {e}")

    def _newRawImage(self, image, lines, samples, bps, axial, lateral, timestamp, jpg, rf, angle):
        pass

    def _newSpectrumImage(self, image, lines, samples, bps, period, micronsPerSample, velocityPerSample, pw):
        pass

    def _newImuData(self, imu):
        pass

    def _buttonsFn(self, button, clicks):
        pass

    def _freezeFn(self, is_frozen):
        with self._lock:
            self._frozen = is_frozen
        print(f"[Clarius] Imaging {'FROZEN' if is_frozen else 'RUNNING'}")

    def start(self) -> bool:
        if not HAS_CAST:
            print("[Clarius] pyclariuscast not available")
            return False

        with self._lock:
            if self._running:
                return True
            self._running = True
            self._last_status = "connecting"

        self._thread = threading.Thread(target=self._run_connect_loop, name="ClariusCastReceiver", daemon=True)
        self._thread.start()
        return True

    def _create_cast(self):
        # Keep strong references to callbacks to prevent garbage collection inside Boost.Python
        self._cb_processed = self._newProcessedImage
        self._cb_raw = self._newRawImage
        self._cb_spectrum = self._newSpectrumImage
        self._cb_imu = self._newImuData
        self._cb_freeze = self._freezeFn
        self._cb_buttons = self._buttonsFn

        return pyclariuscast.Caster(
            self._cb_processed,
            self._cb_raw,
            self._cb_spectrum,
            self._cb_imu,
            self._cb_freeze,
            self._cb_buttons
        )

    def _connect_once(self) -> bool:
        target_ip = self._requested_target_ip
        if target_ip == "auto":
            with self._lock:
                self._last_status = "discovering"
            print(f"[Clarius] Auto-discovering probe on port {self._port}...")
            discovered_ip = self._auto_discover_probe()
            if not discovered_ip:
                with self._lock:
                    self._last_status = "probe not found"
                print("[Clarius] Could not auto-discover probe.")
                return False
            target_ip = discovered_ip
            self._target_ip = discovered_ip
            print(f"[Clarius] Found probe at {self._target_ip}")

        cast = self._create_cast()
        ret = cast.init(self._keys_dir, 640, 480)
        if not ret:
            with self._lock:
                self._last_status = "init failed"
            print("[Clarius] Initialization failed!")
            try:
                cast.destroy()
            except Exception:
                pass
            return False

        with self._lock:
            self._last_status = f"connecting {target_ip}:{self._port}"
        print(f"[Clarius] Connecting to {target_ip}:{self._port}...")
        ret = cast.connect(target_ip, self._port, "research")
        if not ret:
            with self._lock:
                self._last_status = "connection failed"
            print("[Clarius] Connection failed! Make sure App is running and connected.")
            try:
                cast.destroy()
            except Exception:
                pass
            return False

        now = time.monotonic()
        with self._lock:
            if not self._running:
                should_destroy = True
            else:
                should_destroy = False
                self._cast = cast
                self._connected = True
                self._connected_at_monotonic = now
                self._last_status = "connected, waiting for frames"
        if should_destroy:
            try:
                cast.disconnect()
            except Exception:
                pass
            try:
                cast.destroy()
            except Exception:
                pass
            return False
        print("[Clarius] Connected; waiting for live frames")
        return True

    def _run_connect_loop(self) -> None:
        while True:
            with self._lock:
                running = self._running
                connected = self._connected
                connected_at = self._connected_at_monotonic
                has_frame = self._front_frame is not None
                last_frame_monotonic = self._last_frame_monotonic
                frozen = self._frozen
            if not running:
                break

            if connected:
                if not has_frame and (time.monotonic() - connected_at) > CLARIUS_INITIAL_FRAME_TIMEOUT_S:
                    print("[Clarius] Connected but no processed frames arrived; reconnecting.")
                    self._disconnect_current_cast()
                elif (
                    has_frame
                    and not frozen
                    and last_frame_monotonic > 0.0
                    and (time.monotonic() - last_frame_monotonic) > CLARIUS_FRAME_STALE_RECONNECT_S
                ):
                    print("[Clarius] Live frames stopped; reconnecting Cast.")
                    self._disconnect_current_cast()
                else:
                    time.sleep(0.25)
                continue

            self._connect_once()
            time.sleep(CLARIUS_RECONNECT_INTERVAL_S)

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._disconnect_current_cast()

    def _disconnect_current_cast(self) -> None:
        with self._lock:
            cast = self._cast
            self._cast = None
            self._connected = False
            self._front_frame = None
            self._last_frame_monotonic = 0.0
            self._current_fps = 0.0
            if self._running:
                self._last_status = "reconnecting"
            else:
                self._last_status = "stopped"
        if cast is not None:
            try:
                cast.disconnect()
            except Exception:
                pass
            try:
                cast.destroy()
            except Exception:
                pass

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._front_frame
            
    def get_cast(self):
        """Expose the cast object directly for keyboard bindings."""
        with self._lock:
            return self._cast if self._connected else None

    def get_fps(self) -> float:
        with self._lock:
            return self._current_fps

    def get_status_lines(self) -> list[str]:
        with self._lock:
            connected = self._connected
            has_frame = self._front_frame is not None
            last_frame_monotonic = self._last_frame_monotonic
            last_status = self._last_status
            target_ip = self._target_ip
            fps = self._current_fps

        if not connected:
            return [f"Clarius: {last_status}", "Open Clarius app + enable Cast Research"]
        if not has_frame:
            return ["Clarius: connected, waiting for image", "Start imaging on the Clarius app/probe"]

        age = time.monotonic() - last_frame_monotonic if last_frame_monotonic > 0.0 else 0.0
        if age > CLARIUS_FRAME_STALE_WARN_S:
            return [f"Clarius: last frame {age:.1f}s old", "Check freeze/app connection"]
        return [f"Clarius: {target_ip} {fps:.1f} FPS"]

    def is_connection_ok(self, max_frame_age_s: float) -> bool:
        with self._lock:
            connected = self._connected
            has_frame = self._front_frame is not None
            last_frame_monotonic = self._last_frame_monotonic
            frozen = self._frozen
        if not connected or not has_frame:
            return False
        if frozen:
            return True
        if last_frame_monotonic <= 0.0:
            return False
        return (time.monotonic() - last_frame_monotonic) <= max(0.5, max_frame_age_s)


class ForceReceiver:
    """Subscribe to a WrenchStamped topic via rosbridge in a background thread."""

    def __init__(self, host: str, port: int, topic: str) -> None:
        self._host = host
        self._port = port
        self._topic = topic
        self._lock = threading.Lock()
        self._current_kg = 0.0
        self._running = False
        self._client: Optional[roslibpy.Ros] = None
        self._listener: Optional[roslibpy.Topic] = None
        self._thread: Optional[threading.Thread] = None
        self._beep_thread: Optional[threading.Thread] = None
        self._last_message_monotonic = 0.0

    def start(self) -> None:
        if HAS_ROSLIBPY_AUTORECONNECT:
            AutobahnRosBridgeClientFactory.set_initial_delay(FORCE_DIRECT_RECONNECT_DELAY_S)
            AutobahnRosBridgeClientFactory.set_max_delay(FORCE_DIRECT_RECONNECT_DELAY_S)
            AutobahnRosBridgeClientFactory.set_max_retries(None)
        self._client = roslibpy.Ros(host=self._host, port=self._port)
        self._client.on_ready(self._on_ready, run_in_thread=True)
        self._client.on("close", self._on_close)
        self._listener = roslibpy.Topic(
            self._client,
            self._topic,
            "geometry_msgs/WrenchStamped",
            queue_length=1,
            reconnect_on_close=True,
        )
        self._listener.subscribe(self._on_message)
        self._running = True
        if sys.platform == "win32":
            self._beep_thread = threading.Thread(target=self._beep_worker, daemon=True)
            self._beep_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._teardown_client()
        if self._beep_thread is not None:
            self._beep_thread.join(timeout=2.0)
            self._beep_thread = None

    def get_value(self) -> float:
        with self._lock:
            return self._current_kg

    def has_recent_message(self, max_age_s: float) -> bool:
        with self._lock:
            last_message_monotonic = self._last_message_monotonic
        if last_message_monotonic <= 0.0:
            return False
        return (time.monotonic() - last_message_monotonic) <= max(0.1, max_age_s)

    def is_worker_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _run(self) -> None:
        client = self._client
        if client is None:
            return
        print(f"[Force] Connecting to {self._host}:{self._port} and subscribing to {self._topic}")
        try:
            client.run_forever()
        except Exception as exc:
            if self._running:
                print(f"[Force] Receiver loop error: {exc}")

    def _on_ready(self) -> None:
        if self._running:
            print(f"[Force] Direct rosbridge connected: {self._host}:{self._port}")

    def _on_close(self, *args) -> None:
        if self._running:
            print(f"[Force] Direct rosbridge disconnected; waiting for reconnect: {self._host}:{self._port}")

    def _teardown_client(self) -> None:
        listener = self._listener
        client = self._client
        self._listener = None
        self._client = None

        if listener is not None:
            try:
                listener.unsubscribe()
            except Exception:
                pass

        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.terminate()
            except Exception:
                pass

    def _on_message(self, msg: dict) -> None:
        try:
            wrench = msg.get("wrench", {})
            force = wrench.get("force", {})
            fx = float(force.get("x", 0.0))
            fy = float(force.get("y", 0.0))
            fz = float(force.get("z", 0.0))
            mag_kg = math.sqrt(fx * fx + fy * fy + fz * fz) / 9.8
            with self._lock:
                self._current_kg = mag_kg
                self._last_message_monotonic = time.monotonic()
        except Exception as exc:
            print(f"[Force] Decode error: {exc}")

    def _beep_worker(self) -> None:
        """Repeat the force alert while force stays above threshold."""
        _run_force_alert_loop(lambda: self._running, self.get_value)


class ForceBridgeClient:
    """Poll a local HTTP force bridge from a background thread."""

    def __init__(self, url: str, poll_hz: float = 30.0, timeout_s: float = 0.25) -> None:
        self._url = url
        self._poll_interval_s = 1.0 / max(1.0, poll_hz)
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._current_kg = 0.0
        self._running = False
        self._connected = False
        self._last_message_monotonic = 0.0
        self._last_positive_force_monotonic = 0.0
        self._last_error = ""
        self._thread: Optional[threading.Thread] = None
        self._beep_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        if sys.platform == "win32":
            self._beep_thread = threading.Thread(target=self._beep_worker, daemon=True)
            self._beep_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._beep_thread is not None:
            self._beep_thread.join(timeout=2.0)
            self._beep_thread = None

    def get_value(self) -> float:
        with self._lock:
            return self._current_kg

    def has_recent_message(self, max_age_s: float) -> bool:
        with self._lock:
            last_message_monotonic = self._last_message_monotonic
        if last_message_monotonic <= 0.0:
            return False
        return (time.monotonic() - last_message_monotonic) <= max(0.1, max_age_s)

    def is_worker_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _set_state(self, value_kg: float, connected: bool, *, error: str = "", fresh: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            requested_value_kg = max(0.0, float(value_kg))
            if fresh:
                self._current_kg = requested_value_kg
            elif requested_value_kg > 0.0:
                self._current_kg = requested_value_kg
            elif self._current_kg > 0.0 and (now - self._last_positive_force_monotonic) <= FORCE_ENDPOINT_VALUE_HOLD_S:
                pass
            else:
                self._current_kg = 0.0
            self._connected = bool(connected)
            self._last_error = error
            if fresh:
                self._last_message_monotonic = now
            if requested_value_kg > 0.0:
                self._last_positive_force_monotonic = now

    def _run(self) -> None:
        last_reported_connected: Optional[bool] = None
        while self._running:
            loop_started = time.monotonic()
            try:
                payload = _fetch_json(self._url, self._timeout_s)
                force_kg, fresh, status_text = _parse_force_endpoint_payload(payload)

                if fresh:
                    self._set_state(force_kg, True, fresh=True)
                else:
                    self._set_state(0.0, False, error=status_text)

                if last_reported_connected != fresh:
                    if fresh:
                        print(f"[Force] Force endpoint connected: {self._url}")
                    else:
                        print(f"[Force] Force endpoint waiting for live force: {self._url}")
                    last_reported_connected = fresh
            except Exception as exc:
                self._set_state(0.0, False, error=str(exc))
                if last_reported_connected is not False:
                    print(f"[Force] Force endpoint unavailable: {self._url} ({exc})")
                    last_reported_connected = False

            elapsed = time.monotonic() - loop_started
            sleep_s = max(0.0, self._poll_interval_s - elapsed)
            if sleep_s > 0.0:
                time.sleep(sleep_s)

    def _beep_worker(self) -> None:
        """Repeat the force alert while force stays above threshold."""
        _run_force_alert_loop(lambda: self._running, self.get_value)


class VoiceLevelReceiver:
    """Track microphone level via sounddevice and expose a smoothed waveform history."""

    def __init__(
        self,
        input_device: Optional[int | str],
        monitor_device: Optional[int | str],
        source: str,
        sample_rate: int,
        channels: int,
        min_db: float,
        max_db: float,
        smoothing: float,
    ) -> None:
        self._input_device = input_device
        self._monitor_device = monitor_device
        self._source = source.lower()
        self._sample_rate = sample_rate
        self._channels = channels
        self._min_db = min_db
        self._max_db = max_db
        self._smoothing = max(0.0, min(smoothing, 0.999))
        self._lock = threading.Lock()
        self._level = 0.0
        self._history = np.zeros(64, dtype=np.float32)
        self._stream: Optional[sd.InputStream] = None
        self._resolved_source = "input"
        self._resolved_device: Optional[int | str] = None

    def start(self) -> None:
        if not HAS_SOUNDDEVICE:
            raise RuntimeError("sounddevice is not installed")
        stream_kwargs = self._build_stream_kwargs()
        self._stream = sd.InputStream(callback=self._audio_callback, **stream_kwargs)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def get_level(self) -> float:
        with self._lock:
            return self._level

    def describe_source(self) -> str:
        return f"{self._resolved_source} device: {_describe_audio_device(self._resolved_device)}"

    def get_visual_state(self, samples: int) -> tuple[float, np.ndarray]:
        with self._lock:
            level = self._level
            history = self._history.copy()

        if samples > 0 and samples != history.size:
            src = np.linspace(0.0, 1.0, history.size, dtype=np.float32)
            dst = np.linspace(0.0, 1.0, samples, dtype=np.float32)
            history = np.interp(dst, src, history).astype(np.float32)

        if history.size >= 5:
            kernel = np.array([0.08, 0.2, 0.44, 0.2, 0.08], dtype=np.float32)
            history = np.convolve(history, kernel, mode="same")

        return level, history

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            return
        if frames <= 0:
            return

        mono = np.asarray(indata[:, 0], dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))
        peak = float(np.max(np.abs(mono)))
        rms_db = 20.0 * math.log10(max(rms, 1e-7))
        peak_db = 20.0 * math.log10(max(peak, 1e-7))

        rms_norm = (rms_db - self._min_db) / max(self._max_db - self._min_db, 1e-6)
        peak_norm = (peak_db - self._min_db) / max(self._max_db - self._min_db, 1e-6)
        rms_norm = max(0.0, min(rms_norm, 1.0))
        peak_norm = max(0.0, min(peak_norm, 1.0))
        instant = max(rms_norm, peak_norm * 0.92)

        with self._lock:
            self._level = (self._smoothing * self._level) + ((1.0 - self._smoothing) * instant)
            previous = float(self._history[-1])
            if instant >= previous:
                visual = (0.45 * previous) + (0.55 * instant)
            else:
                visual = (0.88 * previous) + (0.12 * instant)
            self._history[:-1] = self._history[1:]
            self._history[-1] = max(0.0, min(visual, 1.0))

    def _build_stream_kwargs(self) -> dict:
        if self._source == "auto":
            try:
                return self._build_monitor_stream_kwargs()
            except RuntimeError:
                return self._build_input_stream_kwargs()

        if self._source in ("monitor", "loopback"):
            return self._build_monitor_stream_kwargs()

        return self._build_input_stream_kwargs()

    def _build_monitor_stream_kwargs(self) -> dict:
        monitor_device, info = _find_monitor_input_device(self._monitor_device)
        channels = max(1, min(self._channels, int(info["max_input_channels"])))
        samplerate = self._sample_rate or int(round(float(info["default_samplerate"])))
        sd.check_input_settings(
            device=monitor_device,
            channels=channels,
            samplerate=samplerate,
            dtype="float32",
        )
        self._resolved_source = "monitor"
        self._resolved_device = monitor_device
        return {
            "samplerate": samplerate,
            "blocksize": 256,
            "device": monitor_device,
            "channels": channels,
            "dtype": "float32",
            "latency": "low",
        }

    def _build_input_stream_kwargs(self) -> dict:
        input_device = self._input_device
        if input_device is None:
            input_device = sd.default.device[0]
        if input_device is None or (isinstance(input_device, int) and input_device < 0):
            raise RuntimeError("No default input device is available for voice capture")

        info = sd.query_devices(input_device)
        channels = max(1, min(self._channels, int(info["max_input_channels"])))
        samplerate = self._sample_rate or int(round(float(info["default_samplerate"])))
        sd.check_input_settings(
            device=input_device,
            channels=channels,
            samplerate=samplerate,
            dtype="float32",
        )
        self._resolved_source = "input"
        self._resolved_device = input_device
        return {
            "samplerate": samplerate,
            "blocksize": 256,
            "device": input_device,
            "channels": channels,
            "dtype": "float32",
            "latency": "low",
        }


def _draw_force_bar(canvas: np.ndarray, value: float, x: int, y: int, w: int, h: int) -> None:
    """Draw the force magnitude as a vertical color bar."""
    n_ch = canvas.shape[2] if canvas.ndim == 3 else 1
    white = (255, 255, 255, 255) if n_ch == 4 else (255, 255, 255)
    border_color = (20, 20, 20, 255) if n_ch == 4 else (20, 20, 20)
    empty_color = (50, 50, 50, 220) if n_ch == 4 else (50, 50, 50)

    ratio = max(0.0, min(value / FORCE_GAUGE_MAX_KG, 1.0))
    inset = max(2, min(w // 4, 4))

    cv2.rectangle(canvas, (x, y), (x + w, y + h), border_color, -1, cv2.LINE_AA)
    cv2.rectangle(
        canvas,
        (x + inset, y + inset),
        (x + w - inset, y + h - inset),
        empty_color,
        -1,
        cv2.LINE_AA,
    )

    inner_w = max(1, w - 2 * inset)
    inner_h = max(1, h - 2 * inset)
    fill_h = int(round(inner_h * ratio))
    if fill_h > 0:
        if ratio < 0.5:
            t = ratio / 0.5
            fill_color = (
                0,
                255,
                int(round(255 * t)),
            )
        else:
            t = (ratio - 0.5) / 0.5
            fill_color = (
                0,
                int(round(255 * (1.0 - t))),
                255,
            )
        if n_ch == 4:
            fill_color = fill_color + (255,)
        cv2.rectangle(
            canvas,
            (x + inset, y + h - inset - fill_h),
            (x + inset + inner_w, y + h - inset),
            fill_color,
            -1,
            cv2.LINE_AA,
        )

    label = f"{value:.2f} kg"
    text_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0]
    text_x = max(0, x - text_size[0] - 8)
    text_y = min(canvas.shape[0] - 8, y + text_size[1] + 4)
    cv2.putText(
        canvas,
        label,
        (text_x, text_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        white,
        1,
        cv2.LINE_AA,
    )


def _draw_filled_rounded_rect(image: np.ndarray, x: int, y: int, w: int, h: int, radius: int, color) -> None:
    radius = max(1, min(radius, w // 2, h // 2))
    cv2.rectangle(image, (x + radius, y), (x + w - radius, y + h), color, -1, cv2.LINE_AA)
    cv2.rectangle(image, (x, y + radius), (x + w, y + h - radius), color, -1, cv2.LINE_AA)
    cv2.circle(image, (x + radius, y + radius), radius, color, -1, cv2.LINE_AA)
    cv2.circle(image, (x + w - radius, y + radius), radius, color, -1, cv2.LINE_AA)
    cv2.circle(image, (x + radius, y + h - radius), radius, color, -1, cv2.LINE_AA)
    cv2.circle(image, (x + w - radius, y + h - radius), radius, color, -1, cv2.LINE_AA)


def _draw_capture_confirmation(canvas: np.ndarray, text: str) -> None:
    if canvas is None or canvas.size == 0 or not text:
        return

    height, width = canvas.shape[:2]
    channels = canvas.shape[2] if canvas.ndim == 3 else 1
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.8, min(height, width) / 720.0)
    thickness = max(2, int(round(font_scale * 2)))
    text_size, baseline = cv2.getTextSize(text, font, font_scale, thickness)
    text_w, text_h = text_size
    padding_x = max(18, int(round(text_w * 0.08)))
    padding_y = max(10, int(round(text_h * 0.45)))
    text_x = max(0, (width - text_w) // 2)
    text_y = max(text_h + padding_y + 18, int(round(height * 0.12)))

    panel_x = max(0, text_x - padding_x)
    panel_y = max(0, text_y - text_h - padding_y)
    panel_w = min(width - panel_x, text_w + (2 * padding_x))
    panel_h = min(height - panel_y, text_h + baseline + (2 * padding_y))
    if panel_w <= 0 or panel_h <= 0:
        return

    roi = canvas[panel_y:panel_y + panel_h, panel_x:panel_x + panel_w]
    overlay = roi.copy()
    panel_color = (12, 18, 16, 235) if channels == 4 else (12, 18, 16)
    _draw_filled_rounded_rect(
        overlay,
        0,
        0,
        panel_w - 1,
        panel_h - 1,
        max(10, panel_h // 4),
        panel_color,
    )
    roi[:] = cv2.addWeighted(roi, 0.35, overlay, 0.65, 0)

    shadow_color = (0, 0, 0, 255) if channels == 4 else (0, 0, 0)
    text_color = (70, 255, 150, 255) if channels == 4 else (70, 255, 150)
    cv2.putText(canvas, text, (text_x + 2, text_y + 2), font, font_scale, shadow_color, thickness + 2, cv2.LINE_AA)
    cv2.putText(canvas, text, (text_x, text_y), font, font_scale, text_color, thickness, cv2.LINE_AA)


def _draw_voice_meter(
    canvas: np.ndarray,
    value: float,
    history: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Draw a simple remote-speaking indicator using white vertical bars."""
    ch, cw = canvas.shape[:2]
    x = max(0, min(x, cw - 2))
    y = max(0, min(y, ch - 2))
    w = max(2, min(w, cw - x))
    h = max(2, min(h, ch - y))

    roi = canvas[y:y + h, x:x + w]
    if roi.ndim != 3 or roi.shape[2] < 3:
        return
    roi_rgb = np.ascontiguousarray(roi[:, :, :3])
    panel = np.zeros((h, w, 3), dtype=np.uint8)
    bars_layer = np.zeros_like(panel)

    radius = max(8, min(w, h) // 6)
    _draw_filled_rounded_rect(panel, 0, 0, w, h, radius, (0, 0, 0))
    roi_rgb[:] = cv2.addWeighted(roi_rgb, 0.62, panel, 0.38, 0)

    bar_count = max(11, min(21, int(history.size)))
    if bar_count <= 0:
        roi[:, :, :3] = roi_rgb
        return

    if history.size != bar_count:
        src = np.linspace(0.0, 1.0, max(1, history.size), dtype=np.float32)
        dst = np.linspace(0.0, 1.0, bar_count, dtype=np.float32)
        bars = np.interp(dst, src, history if history.size > 0 else np.zeros(1, dtype=np.float32))
    else:
        bars = history

    margin_x = max(10, w // 12)
    margin_y = max(8, h // 10)
    inner_w = max(12, w - (2 * margin_x))
    inner_h = max(14, h - (2 * margin_y))
    center_y = margin_y + (inner_h // 2)
    step = inner_w / max(bar_count, 1)
    thickness = max(2, int(round(step * 0.22)))
    max_bar_half = max(6, int(round(inner_h * 0.44)))

    for idx, sample in enumerate(bars):
        sample = float(max(0.0, min(sample, 1.0)))
        x_pos = int(round(margin_x + ((idx + 0.5) * step)))
        bar_half = max(2, int(round(max_bar_half * (0.14 + (sample * 0.92)))))
        if value < 0.03 and sample < 0.03:
            bar_half = 2

        y0 = max(margin_y, center_y - bar_half)
        y1 = min(h - margin_y, center_y + bar_half)
        cv2.line(bars_layer, (x_pos, y0), (x_pos, y1), (255, 255, 255), thickness, cv2.LINE_AA)

    roi_rgb[:] = cv2.addWeighted(roi_rgb, 1.0, bars_layer, 0.95, 0)

    roi[:, :, :3] = roi_rgb


def _overlay_pip(canvas: np.ndarray, pip_frame: np.ndarray, x: int, y: int, border: int = 3) -> None:
    """Stamp pip_frame onto canvas at (x, y) with a dark border. In-place, boundary-safe."""
    ch, cw = canvas.shape[:2]
    ph, pw = pip_frame.shape[:2]
    n_ch = canvas.shape[2] if canvas.ndim == 3 else 1

    bx1, by1 = max(x - border, 0), max(y - border, 0)
    bx2, by2 = min(x + pw + border, cw), min(y + ph + border, ch)
    if bx2 > bx1 and by2 > by1:
        # Draw a bright Cyan border to make the PiP highly visible
        canvas[by1:by2, bx1:bx2] = (255, 255, 0, 255) if n_ch == 4 else (255, 255, 0)

    px1, py1 = max(x, 0), max(y, 0)
    px2, py2 = min(x + pw, cw), min(y + ph, ch)
    sx, sy = px1 - x, py1 - y
    sw, sh = px2 - px1, py2 - py1
    if sw > 0 and sh > 0:
        src = pip_frame[sy:sy + sh, sx:sx + sw]
        if n_ch == 4 and src.ndim == 3 and src.shape[2] == 3:
            alpha = np.full((sh, sw, 1), 255, dtype=src.dtype)
            src = np.concatenate((src, alpha), axis=2)
        canvas[py1:py2, px1:px2] = src


def _draw_text_block(
    canvas: np.ndarray,
    lines: list[str],
    x: int,
    y: int,
    color: tuple[int, int, int] = (0, 255, 255),
    font_scale: float = 0.52,
    thickness: int = 1,
    line_height: int = 18,
) -> None:
    if not lines:
        return
    for idx, line in enumerate(lines):
        baseline_y = y + (idx * line_height)
        cv2.putText(canvas, line, (x, baseline_y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
        cv2.putText(canvas, line, (x, baseline_y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv2.LINE_AA)


def _draw_status_panel(
    canvas: np.ndarray,
    lines: list[str],
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    if canvas is None or canvas.size == 0 or not lines:
        return

    ch, cw = canvas.shape[:2]
    x = max(0, min(x, max(0, cw - 2)))
    y = max(0, min(y, max(0, ch - 2)))
    w = max(2, min(w, cw - x))
    h = max(2, min(h, ch - y))
    roi = canvas[y:y + h, x:x + w]
    overlay = roi.copy()
    channels = canvas.shape[2] if canvas.ndim == 3 else 1
    panel_color = (8, 10, 12, 235) if channels == 4 else (8, 10, 12)
    _draw_filled_rounded_rect(overlay, 0, 0, w - 1, h - 1, max(8, min(w, h) // 8), panel_color)
    roi[:] = cv2.addWeighted(roi, 0.25, overlay, 0.75, 0)

    text_x = x + max(10, w // 18)
    text_y = y + max(28, h // 5)
    _draw_text_block(
        canvas,
        lines,
        text_x,
        text_y,
        color=(0, 255, 255),
        font_scale=max(0.45, min(w, h) / 420.0),
        thickness=1,
        line_height=max(18, h // 6),
    )


def _poll_key() -> int:
    if hasattr(cv2, "pollKey"):
        try:
            return int(cv2.pollKey())
        except Exception:
            pass
    return int(cv2.waitKey(1))


def _fetch_json(url: str, timeout_s: float) -> dict:
    req = urllib_request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib_request.urlopen(req, timeout=max(0.05, timeout_s)) as resp:
        payload = resp.read()
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Bridge response is not a JSON object")
    return data


def _parse_force_endpoint_payload(payload: dict) -> tuple[float, bool, str]:
    """Accept either the local bridge schema or a simpler force-value server schema."""
    force_kg: Optional[float] = None
    for key in ("force_kg", "value_kg", "kg", "force"):
        if key not in payload:
            continue
        try:
            force_kg = float(payload.get(key, 0.0) or 0.0)
            break
        except Exception:
            continue

    if force_kg is None:
        raise ValueError("Force endpoint response is missing a numeric force value")

    status_text = str(payload.get("status", "") or "").strip()
    status_lower = status_text.lower()
    if status_lower in {"stale", "disconnected", "error", "unavailable"}:
        connected = False
    elif force_kg > 0.0:
        connected = True
    else:
        connected = bool(payload.get("connected", True))

    default_status = "connected" if connected else "disconnected"
    if not status_text:
        status_text = default_status

    return max(0.0, force_kg), connected, status_text


def _force_connection_failed(force_receiver: Optional[object], use_force: bool) -> bool:
    if not use_force:
        return False
    if force_receiver is None:
        return True

    is_worker_alive = getattr(force_receiver, "is_worker_alive", None)
    if callable(is_worker_alive) and not is_worker_alive():
        return True

    has_recent_message = getattr(force_receiver, "has_recent_message", None)
    if callable(has_recent_message):
        return not bool(has_recent_message(1.5))
    return False


def _clarius_connection_failed(
    clarius_receiver: Optional[ClariusCastReceiver],
    use_clarius: bool,
) -> bool:
    if not use_clarius:
        return False
    if clarius_receiver is None:
        return True
    return not clarius_receiver.is_connection_ok(CLARIUS_FRAME_STALE_RECONNECT_S)


def _button_signal_paths_failed(paths: list[str | os.PathLike[str]]) -> bool:
    for path_text in paths:
        parent = Path(path_text).expanduser().parent
        if not parent.exists():
            return True
    return False


def _viewer_status_text(
    *,
    zed_connection_ok: bool,
    force_receiver: Optional[object],
    use_force: bool,
    clarius_receiver: Optional[ClariusCastReceiver],
    use_clarius: bool,
    button_signal_paths: list[str | os.PathLike[str]],
) -> str:
    if not zed_connection_ok:
        return "ZED connection failed"
    if _force_connection_failed(force_receiver, use_force):
        return "Force connection failed"
    if _clarius_connection_failed(clarius_receiver, use_clarius):
        return "Clarius connection failed"
    if _button_signal_paths_failed(button_signal_paths):
        return "Button connection failed"
    return "OK"


def _probe_force_bridge(url: str, timeout_s: float) -> bool:
    try:
        _fetch_json(url, timeout_s)
        return True
    except Exception:
        return False


def _parse_force_bridge_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", ""):
        raise ValueError(f"Unsupported force bridge URL scheme: {parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    path = parsed.path or "/force"
    if not path.startswith("/"):
        path = "/" + path
    return host, port, path


def _start_force_bridge_subprocess(args: argparse.Namespace) -> Optional[subprocess.Popen]:
    script_path = SCRIPT_DIR / "force_bridge_server.py"
    if not script_path.exists():
        print(f"[Force] Bridge server script not found: {script_path}", file=sys.stderr)
        return None

    try:
        listen_host, listen_port, listen_path = _parse_force_bridge_url(args.force_bridge_url)
    except Exception as exc:
        print(f"[Force] Invalid bridge URL {args.force_bridge_url!r}: {exc}", file=sys.stderr)
        return None

    cmd = [
        sys.executable,
        str(script_path),
        "--ros-host",
        args.force_host,
        "--ros-port",
        str(args.force_port),
        "--ros-topic",
        args.force_topic,
        "--listen-host",
        listen_host,
        "--listen-port",
        str(listen_port),
        "--listen-path",
        listen_path,
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        print(f"[Force] Failed to start local bridge server: {exc}", file=sys.stderr)
        return None

    deadline = time.monotonic() + max(0.5, args.force_bridge_start_timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"[Force] Local bridge server exited early with code {proc.returncode}", file=sys.stderr)
            return None
        if _probe_force_bridge(args.force_bridge_url, args.force_bridge_timeout):
            print(f"[Force] Local bridge server ready at {args.force_bridge_url}")
            return proc
        time.sleep(0.2)

    print(
        f"[Force] Local bridge server did not respond within {args.force_bridge_start_timeout:.1f}s; "
        "the viewer will keep polling for it",
        file=sys.stderr,
    )
    return proc


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Display the ZED SBS feed full screen with Clarius PiP and an optional force gauge."
    )
    parser.add_argument("--resolution", choices=RESOLUTION_MAP.keys(), default="HD1080")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--swap-eyes", action="store_true", help="Swap left/right order.")
    parser.add_argument("--monitor", type=int, default=-1, help="Monitor index (Windows) to target; -1 = primary/OS choice.")
    parser.add_argument("--output-width", type=int, default=0, help="Optional output width in pixels (even number enforced).")
    parser.add_argument("--output-height", type=int, default=0, help="Optional output height in pixels.")
    parser.add_argument("--window-name", default="ZED SBS Live", help="Title of the display window.")
    parser.add_argument("--profile-components", action="store_true", help="Print rolling per-stage timing summaries for the live viewer.")
    parser.add_argument("--profile-overlay", action="store_true", help="Draw the latest component-profile summary on top of the video.")
    parser.add_argument("--profile-interval", type=float, default=1.0, help="Seconds between component-profile summaries (default: 1.0).")
    parser.add_argument("--exclusive-fullscreen", action="store_true", help="Request exclusive fullscreen after positioning the window (Windows only).")
    parser.add_argument("--list-monitors", action="store_true", help="List detected monitors (Windows) and exit.")

    # Clarius options
    clarius = parser.add_argument_group("Clarius overlay")
    clarius.add_argument("--clarius-ip", default="auto", help="IP of Clarius App. Default: auto")
    clarius.add_argument("--clarius-port", type=int, default=5828, help="Port of Clarius Cast API. Default: 5828")
    clarius.add_argument(
        "--clarius-size",
        type=float,
        default=0.25,
        help="PiP size as a fraction of the full SBS width; the same scale is applied to eye width and height (default: 0.25).",
    )
    clarius.add_argument("--clarius-depth", type=int, default=60, help="Stereo depth shift in pixels (default: 60).")
    clarius.add_argument("--clarius-margin", type=int, default=20, help="Margin from edge in pixels (default: 20).")
    clarius.add_argument(
        "--clarius-toggle-file",
        default=str(DEFAULT_CLARIUS_TOGGLE_STATE_FILE),
        help="Path to a file-based Clarius overlay toggle signal.",
    )
    clarius.add_argument(
        "--clarius-capture-file",
        default=str(DEFAULT_CLARIUS_CAPTURE_STATE_FILE),
        help="Path to a file-based Clarius capture request signal.",
    )
    clarius.add_argument(
        "--clarius-capture-dir",
        default=str(DEFAULT_CLARIUS_CAPTURE_DIR),
        help="Directory where paired Clarius and ZED snapshot PNG files are saved.",
    )
    clarius.add_argument(
        "--clarius-freeze-file",
        default=str(DEFAULT_CLARIUS_FREEZE_STATE_FILE),
        help="Path to a file-based Clarius freeze/unfreeze request signal.",
    )
    clarius.add_argument(
        "--clarius-contrast-up-file",
        default=str(DEFAULT_CLARIUS_CONTRAST_UP_STATE_FILE),
        help="Path to a file-based Clarius contrast-up request signal.",
    )
    clarius.add_argument(
        "--clarius-contrast-down-file",
        default=str(DEFAULT_CLARIUS_CONTRAST_DOWN_STATE_FILE),
        help="Path to a file-based Clarius contrast-down request signal.",
    )
    clarius.add_argument("--no-clarius", action="store_true", help="Disable the Clarius overlay altogether.")

    controls = parser.add_argument_group("External control signals")
    controls.add_argument(
        "--display-toggle-file",
        default=str(DEFAULT_DISPLAY_MODE_TOGGLE_STATE_FILE),
        help="Path to a file-based 2D/3D display-toggle request signal. Each event sends Alt+Q on Windows.",
    )
    controls.add_argument(
        "--button4-placeholder-file",
        default=str(DEFAULT_OPERATOR_BUTTON_4_PLACEHOLDER_STATE_FILE),
        help="Path to a placeholder request signal for operator/JX11 button 4.",
    )
    controls.add_argument(
        "--start-display-mode",
        choices=("3d", "2d"),
        default="2d",
        help="Assumed startup display mode. In 2d mode, only the left eye is shown full screen.",
    )

    force = parser.add_argument_group("Force gauge overlay")
    force.add_argument(
        "--force-source",
        choices=("bridge", "rosbridge", "auto"),
        default="bridge",
        help=(
            "Force input source. 'bridge' uses a local HTTP force bridge server, "
            "'rosbridge' keeps the legacy direct connection inside the viewer, "
            "and 'auto' prefers the local bridge, then falls back to direct rosbridge."
        ),
    )
    force.add_argument(
        "--force-bridge-url",
        default=DEFAULT_FORCE_BRIDGE_URL,
        help="Local HTTP endpoint exposed by force_bridge_server.py (default: http://127.0.0.1:8765/force).",
    )
    force.add_argument(
        "--force-bridge-timeout",
        type=float,
        default=0.08,
        help="HTTP timeout in seconds when polling the local force bridge (default: 0.08).",
    )
    force.add_argument(
        "--force-bridge-poll-fps",
        type=float,
        default=60.0,
        help="Polling rate for the local force bridge client in the viewer (default: 60).",
    )
    force.add_argument(
        "--force-bridge-start-timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for an auto-started local force bridge server to begin responding (default: 3.0).",
    )
    force.add_argument(
        "--no-force-bridge-autostart",
        action="store_true",
        help="Do not auto-start force_bridge_server.py when using bridge mode.",
    )
    force.add_argument(
        "--force-host",
        default="192.168.6.1",
        help="ROS bridge host used by force_bridge_server.py or by legacy direct rosbridge mode.",
    )
    force.add_argument("--force-port", type=int, default=9090, help="ROS bridge port used by the force bridge server.")
    force.add_argument(
        "--force-topic",
        default="/protect/follower_state_controller/F_ext",
        help="ROS topic carrying geometry_msgs/WrenchStamped force data for the force bridge server.",
    )
    force.add_argument("--no-force", action="store_true", help="Disable the force gauge overlay.")

    return parser.parse_args()


# ---------------------------------------------------------------------------
#  main()
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    if sys.platform == "win32":
        _set_dpi_awareness()

    init_params = sl.InitParameters(
        camera_resolution=RESOLUTION_MAP[args.resolution],
        camera_fps=args.fps,
        depth_mode=sl.DEPTH_MODE.NONE,
    )

    if args.list_monitors:
        monitors = _list_monitors()
        if not monitors:
            print("Monitor listing is only available on Windows.")
            return
        for idx, mon in enumerate(monitors):
            print(f"[{idx}] {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']})")
        return

    camera = sl.Camera()
    status = camera.open(init_params)
    if status != sl.ERROR_CODE.SUCCESS:
        print(f"Failed to open ZED camera: {status}", file=sys.stderr)
        sys.exit(1)

    runtime = sl.RuntimeParameters()
    left = sl.Mat()
    right = sl.Mat()

    cam_conf = camera.get_camera_information().camera_configuration
    eye_width = cam_conf.resolution.width
    eye_height = cam_conf.resolution.height
    capture_res = sl.Resolution(eye_width, eye_height)

    placement = _get_monitor_placement(args.monitor)
    dpi_scale = _get_monitor_scale(placement)
    output_width = args.output_width or (placement["width"] if placement else eye_width * 2)
    output_height = args.output_height or (placement["height"] if placement else eye_height)
    if output_width % 2 != 0:
        output_width -= 1

    print(f"Output size: {output_width}x{output_height}; monitor: {args.monitor if placement else 'primary/default'}; dpi scale: {dpi_scale:.2f}")

    cv2.namedWindow(args.window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(args.window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(args.window_name, output_width, output_height)
    _configure_window_on_monitor(args.window_name, placement, output_width, output_height)
    win_rect = _get_window_rect(args.window_name)
    if win_rect:
        if placement and (win_rect["width"] != output_width or win_rect["height"] != output_height):
            scale_w = win_rect["width"] / output_width
            scale_h = win_rect["height"] / output_height
            adj_width = int(round(output_width / scale_w))
            adj_height = int(round(output_height / scale_h))
            _configure_window_on_monitor(args.window_name, placement, adj_width, adj_height)

    print("Press Q or ESC to quit.")
    print("Clarius Binding: [f] freeze, [+] depth up, [-] depth down, [g] gain down, [G] gain up")
    if sys.platform == "win32":
        print("Clarius Overlay Toggle: Alt+W")
        print("Display Mode Toggle: Alt+Q or display-toggle signal")
    pending_fullscreen = True  # Always enforce full screen for 3D monitoring

    # Clarius Receiver Setup
    clarius_receiver: Optional[ClariusCastReceiver] = None
    pip_w = 0
    pip_h = 0
    left_pip_x = 0
    left_pip_y = 0
    right_pip_x = 0
    right_pip_y = 0
    use_clarius = HAS_CAST and not args.no_clarius
    if use_clarius:
        pip_scale = min(args.clarius_size * 2.0, 1.0)
        pip_w = max(int(eye_width * pip_scale), 64)
        pip_h = max(int(eye_height * pip_scale), 48)
        depth_shift = args.clarius_depth
        margin = args.clarius_margin
        
        left_pip_x = eye_width - pip_w - margin + depth_shift
        left_pip_y = margin
        right_pip_x = eye_width - pip_w - margin - depth_shift
        right_pip_y = margin
        
        try:
            clarius_receiver = ClariusCastReceiver(args.clarius_ip, args.clarius_port, (pip_w, pip_h))
            if clarius_receiver.start():
                print(f"[Clarius] PiP {pip_w}x{pip_h}px on {eye_width}x{eye_height} eye; background connector running")
            else:
                clarius_receiver = None
        except Exception as exc:
            print(f"[Clarius] Init failed: {exc}", file=sys.stderr)
            clarius_receiver = None
    elif not args.no_clarius and not HAS_CAST:
        print("[Clarius] pyclariuscast not available. Running without overlay.")

    force_receiver: Optional[object] = None
    force_bridge_process: Optional[subprocess.Popen] = None
    displayed_force_kg = 0.0
    displayed_force_initialized = False
    force_display_fps = 60.0
    force_display_interval = 1.0 / force_display_fps
    last_force_display_update = time.perf_counter()
    use_force = not args.no_force
    if use_force:
        clarius_layout = clarius_receiver is not None
        if clarius_layout:
            gauge_w = max(int(round(pip_w * 0.05)), 8)
            gauge_h = pip_h
            left_gauge_x = left_pip_x + pip_w
            left_gauge_y = left_pip_y
            right_gauge_x = right_pip_x + pip_w
            right_gauge_y = right_pip_y
        else:
            gauge_w = max(int(round(eye_width * 0.05)), 8)
            gauge_h = max(int(round(eye_height * 0.25)), 60)
            left_gauge_x = eye_width - gauge_w - 20
            left_gauge_y = 20
            right_gauge_x = eye_width - gauge_w - 20
            right_gauge_y = 20
        left_gauge_x = max(0, min(left_gauge_x, eye_width - gauge_w))
        right_gauge_x = max(0, min(right_gauge_x, eye_width - gauge_w))
        left_gauge_y = max(0, min(left_gauge_y, eye_height - gauge_h))
        right_gauge_y = max(0, min(right_gauge_y, eye_height - gauge_h))
        bridge_mode_requested = args.force_source in ("bridge", "auto")
        direct_mode_requested = args.force_source in ("rosbridge", "auto")

        if bridge_mode_requested and not _probe_force_bridge(args.force_bridge_url, args.force_bridge_timeout):
            if not args.no_force_bridge_autostart:
                force_bridge_process = _start_force_bridge_subprocess(args)

        if args.force_source == "bridge" or _probe_force_bridge(args.force_bridge_url, args.force_bridge_timeout):
            try:
                force_receiver = ForceBridgeClient(
                    args.force_bridge_url,
                    poll_hz=args.force_bridge_poll_fps,
                    timeout_s=args.force_bridge_timeout,
                )
                force_receiver.start()
                print(f"[Force] Using local bridge: {args.force_bridge_url}")
            except Exception as exc:
                print(f"[Force] Local bridge client init failed: {exc}", file=sys.stderr)
                force_receiver = None

        if force_receiver is None and direct_mode_requested:
            if HAS_ROSLIBPY:
                try:
                    force_receiver = ForceReceiver(args.force_host, args.force_port, args.force_topic)
                    force_receiver.start()
                    print(f"[Force] Direct rosbridge mode on {args.force_host}:{args.force_port} topic {args.force_topic}")
                except Exception as exc:
                    print(f"[Force] Direct rosbridge init failed: {exc}", file=sys.stderr)
                    force_receiver = None
            elif args.force_source == "rosbridge":
                print("[Force] roslibpy not available. Direct rosbridge mode cannot start.", file=sys.stderr)

        if force_receiver is None:
            if args.force_source == "bridge":
                print(
                    f"[Force] Local bridge not available at {args.force_bridge_url}. "
                    "The force bar will stay at 0 until the bridge comes up.",
                    file=sys.stderr,
                )
            elif args.force_source == "auto":
                print("[Force] No force source became available. Running without live force input.", file=sys.stderr)

    zed_frame_count = 0
    zed_last_fps_time = time.time()
    zed_fps = 0.0

    last_clarius_frame = None
    cached_pip = None
    clarius_overlay_visible = clarius_receiver is not None
    clarius_overlay_off_notice_until = 0.0
    display_mode = args.start_display_mode.lower()
    display_toggle_reader = StateFileEventReader(args.display_toggle_file)
    clarius_toggle_reader = StateFileEventReader(args.clarius_toggle_file)
    clarius_capture_reader = StateFileEventReader(args.clarius_capture_file)
    clarius_freeze_reader = StateFileEventReader(args.clarius_freeze_file)
    clarius_contrast_up_reader = StateFileEventReader(args.clarius_contrast_up_file)
    clarius_contrast_down_reader = StateFileEventReader(args.clarius_contrast_down_file)
    button4_placeholder_reader = StateFileEventReader(args.button4_placeholder_file)
    button_signal_paths = [
        args.display_toggle_file,
        args.clarius_toggle_file,
        args.clarius_capture_file,
        args.clarius_freeze_file,
        args.clarius_contrast_up_file,
        args.clarius_contrast_down_file,
        args.button4_placeholder_file,
    ]
    display_toggle_cooldown_sec = 1.5
    clarius_toggle_cooldown_sec = 1.0
    clarius_capture_cooldown_sec = 1.0
    button4_placeholder_cooldown_sec = 1.0
    last_display_toggle_at = 0.0
    last_clarius_toggle_at = 0.0
    last_clarius_capture_at = 0.0
    last_button4_placeholder_at = 0.0
    clarius_capture_dir = Path(args.clarius_capture_dir)
    capture_confirmation_until = 0.0
    clarius_contrast = 1.0
    clarius_contrast_step = 0.15
    clarius_min_contrast = 0.55
    clarius_max_contrast = 2.20
    alt_w_was_down = False
    alt_q_was_down = False
    suppress_q_quit_until = 0.0
    sbs_compose_buffer: Optional[np.ndarray] = None
    output_frame_buffer: Optional[np.ndarray] = None
    zed_connection_ok = True
    zed_grab_failure_count = 0
    profile_enabled = args.profile_components or args.profile_overlay
    profiler = ComponentProfiler(profile_enabled, args.profile_interval)
    if profile_enabled:
        print(f"[Profile] Component profiling enabled; report interval {profiler.report_interval:.2f}s")

    try:
        while True:
            loop_start = time.perf_counter()

            with profiler.section("grab"):
                grab_status = camera.grab(runtime)
            if grab_status != sl.ERROR_CODE.SUCCESS:
                zed_grab_failure_count += 1
                if zed_grab_failure_count >= 30:
                    zed_connection_ok = False
                continue
            zed_grab_failure_count = 0
            zed_connection_ok = True
                
            zed_frame_count += 1
            now = time.time()
            if now - zed_last_fps_time >= 1.0:
                zed_fps = zed_frame_count / (now - zed_last_fps_time)
                zed_frame_count = 0
                zed_last_fps_time = now

            with profiler.section("retrieve"):
                camera.retrieve_image(left, sl.VIEW.LEFT, sl.MEM.CPU, capture_res)
                camera.retrieve_image(right, sl.VIEW.RIGHT, sl.MEM.CPU, capture_res)

            left_img = left.get_data()[:, :eye_width, :]
            right_img = right.get_data()[:, :eye_width, :]

            if args.swap_eyes:
                left_img, right_img = right_img, left_img

            with profiler.section("controls"):
                now_monotonic = time.monotonic()
                if display_toggle_reader.poll_toggle():
                    if (now_monotonic - last_display_toggle_at) >= display_toggle_cooldown_sec:
                        last_display_toggle_at = now_monotonic
                        if _trigger_display_mode_toggle():
                            display_mode = "2d" if display_mode == "3d" else "3d"
                            suppress_q_quit_until = now_monotonic + 0.6
                            print("[Display] Sent Alt+Q toggle (file)")
                            print(f"[Display] Viewer mode -> {display_mode.upper()}")
                        else:
                            print("[Display] Display toggle requested, but Alt+Q send failed", file=sys.stderr)

                if clarius_receiver is not None and clarius_toggle_reader.poll_toggle():
                    if (now_monotonic - last_clarius_toggle_at) >= clarius_toggle_cooldown_sec:
                        last_clarius_toggle_at = now_monotonic
                        clarius_overlay_visible = not clarius_overlay_visible
                        clarius_overlay_off_notice_until = (
                            now_monotonic + CLARIUS_OVERLAY_OFF_NOTICE_SECONDS
                            if not clarius_overlay_visible
                            else 0.0
                        )
                        print(
                            f"[Clarius] Overlay {'ON' if clarius_overlay_visible else 'OFF'} (file)"
                        )

                if clarius_receiver is not None and clarius_capture_reader.poll_toggle():
                    if (now_monotonic - last_clarius_capture_at) >= clarius_capture_cooldown_sec:
                        last_clarius_capture_at = now_monotonic
                        capture_frame = clarius_receiver.get_frame()
                        capture_timestamp = _make_capture_timestamp()
                        saved_path = _save_clarius_snapshot(
                            capture_frame,
                            clarius_capture_dir,
                            timestamp=capture_timestamp,
                        )
                        if saved_path is not None:
                            zed_path = _save_zed_sbs_snapshot(
                                left_img,
                                right_img,
                                clarius_capture_dir,
                                capture_timestamp,
                            )
                            capture_confirmation_until = now_monotonic + CAPTURE_CONFIRMATION_SECONDS
                            _play_camera_click_sound()
                            if zed_path is not None:
                                print(f"[Capture] Saved Clarius: {saved_path} | ZED: {zed_path}")
                            else:
                                print(f"[Capture] Saved Clarius: {saved_path} | ZED save failed")
                        else:
                            print("[Clarius] Snapshot request ignored: no frame available")

                if clarius_receiver is not None and clarius_receiver.get_cast():
                    cast = clarius_receiver.get_cast()
                    if clarius_freeze_reader.poll_toggle():
                        cast.userFunction(1, 0)
                        print("[Clarius] Freeze/Unfreeze (file)")

                if clarius_receiver is not None:
                    if clarius_contrast_up_reader.poll_toggle():
                        clarius_contrast = min(
                            clarius_max_contrast,
                            clarius_contrast + clarius_contrast_step,
                        )
                        print(f"[Clarius] Contrast Up -> {clarius_contrast:.2f}")
                    if clarius_contrast_down_reader.poll_toggle():
                        clarius_contrast = max(
                            clarius_min_contrast,
                            clarius_contrast - clarius_contrast_step,
                        )
                        print(f"[Clarius] Contrast Down -> {clarius_contrast:.2f}")

                if button4_placeholder_reader.poll_toggle():
                    if (now_monotonic - last_button4_placeholder_at) >= button4_placeholder_cooldown_sec:
                        last_button4_placeholder_at = now_monotonic
                        print("[Buttons] Placeholder action triggered (button 4)")

            # Process Clarius Overlay
            with profiler.section("clarius"):
                if clarius_receiver is not None and clarius_overlay_visible:
                    pip_frame = clarius_receiver.get_frame()
                    if pip_frame is not None:
                        # Cache the resize operation so we don't recalculate it if the Caster hasn't updated its frame
                        if pip_frame is not last_clarius_frame:
                            cached_pip = cv2.resize(pip_frame, (pip_w, pip_h), interpolation=cv2.INTER_LINEAR)
                            last_clarius_frame = pip_frame
                        
                        if cached_pip is not None:
                            display_pip = _apply_image_contrast(cached_pip, clarius_contrast)
                            
                            # Draw without boundary color
                            _overlay_pip(left_img, display_pip, left_pip_x, left_pip_y, border=0)
                            _overlay_pip(right_img, display_pip, right_pip_x, right_pip_y, border=0)
                            
                            # Add FROZEN text to the display when relevant
                            if clarius_receiver._frozen:
                                cv2.putText(left_img, "FROZEN", (left_pip_x + 10, left_pip_y + 30), 
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                                cv2.putText(right_img, "FROZEN", (right_pip_x + 10, right_pip_y + 30), 
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                    else:
                        status_lines = clarius_receiver.get_status_lines()
                        _draw_status_panel(left_img, status_lines, left_pip_x, left_pip_y, pip_w, min(pip_h, max(96, pip_h // 3)))
                        _draw_status_panel(right_img, status_lines, right_pip_x, right_pip_y, pip_w, min(pip_h, max(96, pip_h // 3)))

            with profiler.section("force"):
                if force_receiver is not None:
                    now_perf = time.perf_counter()
                    raw_force_kg = force_receiver.get_value() if force_receiver is not None else 0.0
                    if not displayed_force_initialized:
                        displayed_force_kg = raw_force_kg
                        displayed_force_initialized = True
                        last_force_display_update = now_perf
                    elif now_perf - last_force_display_update >= force_display_interval:
                        dt = now_perf - last_force_display_update
                        display_smoothing_s = (
                            FORCE_DISPLAY_ATTACK_S
                            if raw_force_kg >= displayed_force_kg
                            else FORCE_DISPLAY_RELEASE_S
                        )
                        alpha = 1.0 - math.exp(-dt / max(display_smoothing_s, 1e-6))
                        displayed_force_kg += alpha * (raw_force_kg - displayed_force_kg)
                        last_force_display_update = now_perf
                    _draw_force_bar(left_img, displayed_force_kg, left_gauge_x, left_gauge_y, gauge_w, gauge_h)
                    _draw_force_bar(right_img, displayed_force_kg, right_gauge_x, right_gauge_y, gauge_w, gauge_h)

            with profiler.section("hud"):
                status_text = _viewer_status_text(
                    zed_connection_ok=zed_connection_ok,
                    force_receiver=force_receiver,
                    use_force=use_force,
                    clarius_receiver=clarius_receiver,
                    use_clarius=use_clarius,
                    button_signal_paths=button_signal_paths,
                )
                hud_text = f"Mode: {display_mode.upper()} | Status: {status_text}"
                hud_color = (0, 255, 0) if status_text == "OK" else (0, 120, 255)
                cv2.putText(left_img, hud_text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(right_img, hud_text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(left_img, hud_text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, hud_color, 2, cv2.LINE_AA)
                cv2.putText(right_img, hud_text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, hud_color, 2, cv2.LINE_AA)
                if args.profile_overlay:
                    profile_lines = profiler.get_overlay_lines()
                    _draw_text_block(left_img, profile_lines, 20, 66)
                    _draw_text_block(right_img, profile_lines, 20, 66)
                if (
                    clarius_receiver is not None
                    and not clarius_overlay_visible
                    and time.monotonic() < clarius_overlay_off_notice_until
                ):
                    overlay_lines = ["Clarius overlay OFF", "Press Alt+W or overlay button"]
                    _draw_text_block(left_img, overlay_lines, 20, 92, color=(0, 255, 255))
                    _draw_text_block(right_img, overlay_lines, 20, 92, color=(0, 255, 255))
                if time.monotonic() < capture_confirmation_until:
                    _draw_capture_confirmation(left_img, CAPTURE_CONFIRMATION_TEXT)
                    _draw_capture_confirmation(right_img, CAPTURE_CONFIRMATION_TEXT)

            with profiler.section("compose"):
                if display_mode == "2d":
                    composed_frame = left_img
                else:
                    compose_shape = (left_img.shape[0], left_img.shape[1] + right_img.shape[1], left_img.shape[2])
                    if sbs_compose_buffer is None or sbs_compose_buffer.shape != compose_shape or sbs_compose_buffer.dtype != left_img.dtype:
                        sbs_compose_buffer = np.empty(compose_shape, dtype=left_img.dtype)

                    left_w = left_img.shape[1]
                    sbs_compose_buffer[:, :left_w] = left_img
                    sbs_compose_buffer[:, left_w:] = right_img
                    composed_frame = sbs_compose_buffer

                sbs_frame = composed_frame
                if sbs_frame.shape[1] != output_width or sbs_frame.shape[0] != output_height:
                    output_shape = (output_height, output_width, sbs_frame.shape[2])
                    if output_frame_buffer is None or output_frame_buffer.shape != output_shape or output_frame_buffer.dtype != sbs_frame.dtype:
                        output_frame_buffer = np.empty(output_shape, dtype=sbs_frame.dtype)
                    cv2.resize(sbs_frame, (output_width, output_height), dst=output_frame_buffer, interpolation=cv2.INTER_LINEAR)
                    sbs_frame = output_frame_buffer
            
            with profiler.section("display"):
                cv2.imshow(args.window_name, sbs_frame)

                if pending_fullscreen:
                    cv2.setWindowProperty(args.window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                    _configure_window_on_monitor(args.window_name, placement, output_width, output_height)
                    pending_fullscreen = False

            should_quit = False
            with profiler.section("input"):
                input_now_monotonic = time.monotonic()
                alt_w_down = _is_alt_w_pressed()
                if alt_w_down and not alt_w_was_down and clarius_receiver is not None:
                    clarius_overlay_visible = not clarius_overlay_visible
                    clarius_overlay_off_notice_until = (
                        input_now_monotonic + CLARIUS_OVERLAY_OFF_NOTICE_SECONDS
                        if not clarius_overlay_visible
                        else 0.0
                    )
                    print(
                        f"[Clarius] Overlay {'ON' if clarius_overlay_visible else 'OFF'}"
                    )
                alt_w_was_down = alt_w_down

                alt_q_down = _is_alt_q_pressed()
                if alt_q_down and not alt_q_was_down:
                    display_mode = "2d" if display_mode == "3d" else "3d"
                    suppress_q_quit_until = input_now_monotonic + 0.6
                    print(f"[Display] Viewer mode -> {display_mode.upper()} (Alt+Q)")
                alt_q_was_down = alt_q_down

                key = _poll_key() & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    if key == 27 or input_now_monotonic >= suppress_q_quit_until:
                        should_quit = True
                elif clarius_receiver and clarius_receiver.get_cast():
                    cast = clarius_receiver.get_cast()
                    if key == ord('f') or key == ord('F'):
                        cast.userFunction(1, 0) # Freeze/Unfreeze
                    elif key == ord('+') or key == ord('='):
                        cast.userFunction(5, 0) # Depth Up
                    elif key == ord('-') or key == ord('_'):
                        cast.userFunction(4, 0) # Depth Down
                    elif key == ord('g'):
                        cast.userFunction(6, 0) # Gain Down
                    elif key == ord('G'):
                        cast.userFunction(7, 0) # Gain Up

            profiler.record("loop", time.perf_counter() - loop_start)
            profiler.finish_iteration()
            if should_quit:
                break
    finally:
        if clarius_receiver is not None:
            clarius_receiver.stop()
        if force_receiver is not None:
            force_receiver.stop()
        if force_bridge_process is not None:
            try:
                force_bridge_process.terminate()
                force_bridge_process.wait(timeout=2.0)
            except Exception:
                try:
                    force_bridge_process.kill()
                except Exception:
                    pass
        camera.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
