# ZED SBS Fullscreen Viewer

Render a live side-by-side (SBS) feed from a Stereolabs ZED camera into a borderless
OpenCV window, intended for 3D displays that accept SBS input (for example, Lenovo 27 3D).

## Features
- Side-by-side left/right live view.
- Monitor selection and DPI-aware placement on Windows.
- Optional exclusive fullscreen and eye swap.
- **Clarius ultrasound overlay** — picture-in-picture via ROS 2, with stereoscopic depth.

## Requirements
- Windows 10/11 recommended (monitor targeting is Windows-only).
- Stereolabs ZED camera and ZED SDK installed.
- Python 3.10+.
- GPU drivers compatible with the ZED SDK.
- *(Optional, for Clarius overlay)* A ROS 2 environment with `rclpy`, `cv_bridge`, and `sensor_msgs` available.

## Setup
1. Create and activate a virtual environment.
2. Install Python dependencies.
3. Install the ZED Python wheel shipped with the SDK.

Example (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install "C:\Program Files (x86)\ZED SDK\python\pyzed\pyzed-5.1-cp312-cp312-win_amd64.whl"
```

Adjust the wheel path to match your SDK version and Python version.

## Usage

List monitors (Windows):

Quick run:

```powershell
python sbs_fullscreen_view.py --monitor 0 --output-width 3840 --output-height 2160 --exclusive-fullscreen --fps 15 --resolution HD2K
```

```powershell
python sbs_fullscreen_view.py --list-monitors
```

Start SBS fullscreen on monitor 0 at 3840x2160:

```powershell
python sbs_fullscreen_view.py --monitor 0 --output-width 3840 --output-height 2160 --exclusive-fullscreen --fps 15 --resolution HD2K
```

Swap eyes if the 3D display is reversed:

```powershell
python sbs_fullscreen_view.py --swap-eyes
```

`--swap-eyes` flips the left/right images before they're stitched into the side-by-side frame.

Press Q or ESC to exit.

Run `python sbs_fullscreen_view.py --help` to see all options.

## Clarius Ultrasound Overlay

When a ROS 2 environment is available, the viewer can subscribe to a Clarius
ultrasound image topic and display it as a picture-in-picture (PiP) overlay on
the **top-right corner** of each eye. The overlay has a configurable stereo
depth offset so it appears to **float in front** of the 3D scene.

### Prerequisites

- A running Clarius ROS 2 driver publishing `sensor_msgs/msg/Image` (e.g.,
  `clarius_ros2`).
- `rclpy`, `cv_bridge`, and `sensor_msgs` must be importable (source your
  ROS 2 workspace before running the script).

### Clarius CLI Options

| Argument | Default | Description |
|---|---|---|
| `--clarius-topic` | `/clarius/image_raw` | ROS 2 image topic |
| `--clarius-size` | `0.25` | PiP width as a fraction of eye width (0.1–0.5) |
| `--clarius-depth` | `20` | Stereo depth shift in pixels (+ = in front) |
| `--clarius-margin` | `20` | Margin from edge in pixels |
| `--no-clarius` | *(flag)* | Disable the overlay |

### Example

```powershell
# Full 3D view with Clarius overlay on monitor 0
python sbs_fullscreen_view.py --monitor 0 --output-width 3840 --output-height 2160 \
    --exclusive-fullscreen --fps 15 --resolution HD2K \
    --clarius-topic /clarius/image_raw --clarius-size 0.3 --clarius-depth 25
```

### How It Works

- A **background thread** spins the ROS 2 subscriber and pre-scales incoming
  frames to PiP dimensions.
- The main loop reads the **latest frame** via a thread-safe double buffer.
  If the Clarius publishes slower than the ZED, the last frame is held — **no
  flicker**.
- The PiP is stamped at slightly different horizontal offsets on each eye
  (`±clarius-depth` pixels), creating binocular disparity that makes the
  overlay appear to float in front of the 3D scene.

## Lenovo 3D monitor steps (ZED camera)
1. Use a Windows machine with an NVIDIA GPU.
2. Connect the ZED camera and the Lenovo 3D monitor to the machine.
3. Install the ZED SDK and Lenovo 3D Explorer.
4. Open Lenovo 3D Explorer.
5. Run `C:\Users\User\Downloads\camera_streaming\single_sender\ZED_SBS_View\sbs_fullscreen_view.py`.
6. The SBS feed opens. Press Alt+Q; the monitor switches to 3D mode.
7. Focus near the center of the monitor for a few seconds until eye tracking locks in.

## Notes
- If the window is not positioned or sized correctly on high-DPI displays, try setting
  `--output-width/--output-height` explicitly or set Windows display scaling to 100%.
- Exclusive fullscreen is only available on Windows.
- If ROS 2 is not installed, the viewer runs normally without the Clarius overlay and prints a warning.

## Force Bridge

The force bar now has a cleaner recommended path:

- `force_bridge_server.py` owns the ROS bridge subscription.
- `sbs_cast_view.py` only polls a local HTTP endpoint for the latest `force_kg`.
- If the force bridge is disconnected or stale, it serves `0.0 kg`.
- The force bridge reconnects to ROS every `5` seconds by default.

### Recommended one-step viewer launch

`sbs_cast_view.py` now defaults to `--force-source bridge` and will auto-start the
local force bridge server if needed:

```powershell
python sbs_cast_view.py --force-host 192.168.6.1 --force-port 9090 --force-topic /protect/follower_state_controller/F_ext
```

### Manual bridge launch

If you want to run the bridge as its own visible process:

```powershell
python force_bridge_server.py --ros-host 192.168.6.1 --ros-port 9090 --ros-topic /protect/follower_state_controller/F_ext
python sbs_cast_view.py --force-source bridge --force-bridge-url http://127.0.0.1:8765/force
```

### Legacy direct mode

The old in-viewer rosbridge connection is still available, but it is now the fallback path:

```powershell
python sbs_cast_view.py --force-source rosbridge --force-host 192.168.6.1 --force-port 9090 --force-topic /protect/follower_state_controller/F_ext
```

## Component Profiling

If the live viewer FPS drops, you can profile the main loop stages directly:

```powershell
python sbs_cast_view.py --profile-components --profile-overlay
```

This reports rolling average timings for:
- `grab`
- `retrieve`
- `controls`
- `clarius`
- `force`
- `hud`
- `compose`
- `display`
- `input`
- total `loop`

Useful options:
- `--profile-components` prints timing summaries to the console.
- `--profile-overlay` draws the latest summary on the viewer itself.
- `--profile-interval 1.0` changes the rolling report window in seconds.

## JX11 Button Map

The JX11 mapper on this machine writes small request files that `sbs_cast_view.py`
consumes at runtime. This is more reliable than synthetic hotkeys for the 3D viewer.

### Current button actions

| Control | Mapping name | Function | Request / action | Description |
|---|---|---|---|---|
| Button | `toggle_2d_3d` | 2D/3D display toggle | `display_mode_toggle.json` | Shared display-mode toggle request. The viewer sends `Alt+Q` on Windows when this file changes. |
| Button | `alt_w_button` | Clarius overlay toggle | `clarius_overlay_toggle.json` | Show or hide the Clarius PiP overlay without disconnecting the Cast session. |
| Button | `capture_clarius_button` | Clarius + ZED snapshot | `clarius_capture_request.json` | Save timestamp-matched Clarius and ZED SBS PNG files in `clarius_captures`, show `CAPTURED`, and play a camera click. |
| Button | `freeze_clarius_button` | Clarius freeze / unfreeze | `clarius_freeze_request.json` | Toggle the Clarius imaging state between live and frozen. This physical button is also the device's `Volume Up` consumer-control key, so the mapper restores the previous system volume after the press. |
| Scroller up | `scroll_up` | Clarius contrast up | `clarius_contrast_up_request.json` | Increase the software contrast applied to the displayed Clarius PiP. |
| Scroller down | `scroll_down` | Clarius contrast down | `clarius_contrast_down_request.json` | Decrease the software contrast applied to the displayed Clarius PiP. |

The `freeze_clarius_button` report is `3,233,0`, which Windows also interprets as
`Volume Up`. The mapper compensates by restoring the prior system volume almost
immediately after the button press, but Windows may still show a brief volume OSD.
If you want to eliminate that side effect entirely, use a different physical button.

### Viewer keyboard controls

| Key | Function | Description |
|---|---|---|
| `Alt+W` | Overlay toggle | Manual fallback for showing or hiding the Clarius PiP. |
| `F` | Freeze / unfreeze | Send the native Clarius freeze toggle. |
| `+` / `=` | Depth up | Increase Clarius imaging depth on the probe/app side. |
| `-` / `_` | Depth down | Decrease Clarius imaging depth on the probe/app side. |
| `g` | Gain down | Decrease native Clarius gain on the probe/app side. |
| `G` | Gain up | Increase native Clarius gain on the probe/app side. |
| `Q` or `Esc` | Quit | Close the viewer. |

## ROS Operator Buttons

`operator_handle_button_bridge.py` watches these ROS topics and writes the same request
files used by the JX11 mapper:

| ROS topic | Function | Request file |
|---|---|---|
| `/operator_handle/button_1` | 2D/3D display toggle | `display_mode_toggle.json` |
| `/operator_handle/button_2` | Clarius overlay on/off | `clarius_overlay_toggle.json` |
| `/operator_handle/button_3` | Clarius screenshot | `clarius_capture_request.json` |
| `/operator_handle/button_4` | Placeholder only | `operator_button_4_placeholder.json` |

This means either source can trigger the same action:
- JX11 button press
- ROS `/operator_handle/button_n` message

## Files
- `sbs_cast_view.py` - main viewer script (ZED + Clarius overlay + force gauge).
- `force_bridge_server.py` - local HTTP bridge for force data from ROS bridge.
- `operator_handle_button_bridge.py` - ROS button bridge for `/operator_handle/button_1..4`.
- `requirements.txt` - Python dependencies (excluding the ZED SDK wheel and ROS 2 packages).
