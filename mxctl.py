#!/usr/bin/env python3
"""
mxctl - Lightweight MX Master 3S controller for Linux

Provides:
  - Arbitrary button remapping (e.g., thumb button -> Home)
  - True ratchet/free-spin scroll toggle via the Smart Shift button
    (replaces the annoying adaptive SmartShift behavior)

Requires: python3, evdev, running as root (or with udev rules for input/hidraw access)
"""

import argparse
import json
import os
import select
import signal
import struct
import sys
import time
from pathlib import Path

try:
    import evdev
    from evdev import UInput, ecodes, InputEvent
except ImportError:
    print("Error: 'evdev' package required. Install with: pip install evdev")
    sys.exit(1)


# --- Constants ---

LOGITECH_VID = 0x046D
BOLT_PIDS = [0xC548]  # Bolt receiver
MX_MASTER_3S_WPID = 0xB034

# HID++ report IDs
HIDPP_SHORT = 0x10  # 7 bytes
HIDPP_LONG = 0x11   # 20 bytes

# HID++ feature IDs
FEAT_ROOT = 0x0000
FEAT_REPROG_CONTROLS_V4 = 0x1B04
FEAT_SMART_SHIFT = 0x2110
FEAT_HIRES_WHEEL = 0x2121

# MX Master 3S control IDs (from REPROG_CONTROLS_V4)
CID_SMART_SHIFT = 0x00C4  # The scroll wheel mode toggle button

# Default config
DEFAULT_CONFIG = {
    "mouse_name_match": "Logitech USB Receiver Mouse",
    "hidraw_vendor_id": "046d",
    "bolt_device_index": None,  # auto-detect
    "button_map": {
        # evdev button code -> target key code
        # Use `mxctl.py --identify` to find your button codes
        # MX Master 3S buttons through Bolt:
        #   BTN_FORWARD (0x115 / 277) = thumb/gesture button
        #   BTN_SIDE (0x113 / 275) = back button
        #   BTN_EXTRA (0x114 / 276) = forward button
        "277": "KEY_LEFTMETA"  # Thumb button -> Super/Win key
    },
    "scroll_toggle_button": None,  # evdev code for scroll toggle, or null to use HID++ Smart Shift diversion
    "divert_smartshift": True,  # Divert the Smart Shift button via HID++ so we can intercept it

    # Hold-and-swipe gestures (like Mac's 3-finger workspace swipe).
    # Hold the gesture_button and swipe horizontally to fire a gesture.
    # If you don't swipe (motion < threshold), the button's mapping in button_map
    # fires as a regular tap.
    "gesture_button": 277,  # evdev button code that activates gesture mode (thumb button)
    "gesture_threshold": 80,  # accumulated px in a direction needed to trigger
    "gestures": {
        # direction -> list of keys to press together (chord)
        "left":  ["KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFT"],   # workspace left
        "right": ["KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_RIGHT"],  # workspace right
        "up":    [],   # leave empty to disable
        "down":  []
    }
}

CONFIG_PATH = Path("~/.config/mxctl/config.json").expanduser()


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # Merge with defaults
        merged = {**DEFAULT_CONFIG, **cfg}
        return merged
    return DEFAULT_CONFIG.copy()


def save_default_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    print(f"Default config written to {CONFIG_PATH}")


# --- Device Discovery ---

def find_mouse_evdev(name_match):
    """Find the MX Master 3S evdev input device."""
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            if name_match.lower() in dev.name.lower():
                # Verify it has mouse buttons
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps and ecodes.EV_REL in caps:
                    return dev
                dev.close()
        except (PermissionError, OSError):
            continue
    return None


def find_hidraw_device(vendor_id_hex):
    """Find the HID++ vendor-specific hidraw device for the Bolt receiver."""
    vendor_id_hex = vendor_id_hex.lower()
    for hidraw in sorted(Path("/sys/class/hidraw").iterdir()):
        try:
            uevent = (hidraw / "device" / "uevent").read_text()
            if vendor_id_hex not in uevent.lower():
                continue
            # Check if this is the vendor-specific interface (usage page FF00)
            rdesc_path = hidraw / "device" / "report_descriptor"
            rdesc = rdesc_path.read_bytes()
            # Vendor usage page 0xFF00 starts with 06 00 FF
            if b'\x06\x00\xff' in rdesc:
                dev_path = f"/dev/{hidraw.name}"
                return dev_path
        except (PermissionError, OSError):
            continue
    return None


def find_bolt_device_index(hidraw_path, timeout=5.0):
    """Auto-detect which Bolt receiver slot has the MX Master 3S.

    Sends HID++ pings to each slot and checks for a valid response.
    The mouse must be awake (recently moved) for this to work.
    """
    try:
        fd = os.open(hidraw_path, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        print(f"Permission denied: {hidraw_path}")
        print("Run as root or install the udev rules (see --install-udev)")
        return None

    # Drain pending data
    while select.select([fd], [], [], 0.05)[0]:
        os.read(fd, 64)

    found_idx = None
    for dev_idx in range(1, 7):
        # IRoot.Ping: long report, feature index 0x00, funcId=1, sw_id=0x0A
        msg = bytes([HIDPP_LONG, dev_idx, 0x00, 0x1A, 0x00]) + bytes(15)
        try:
            os.write(fd, msg)
        except OSError:
            continue
        time.sleep(0.1)
        r, _, _ = select.select([fd], [], [], 0.3)
        while r:
            resp = os.read(fd, 64)
            if len(resp) >= 7:
                if resp[2] == 0x8F:
                    # Error response
                    pass
                else:
                    # Valid response - this device index is alive
                    # Check if it's the mouse by querying device name
                    found_idx = dev_idx
                    break
            r, _, _ = select.select([fd], [], [], 0.1)
        if found_idx:
            break

    os.close(fd)
    return found_idx


# --- HID++ Protocol ---

class HidppDevice:
    """Communicate with a Logitech device via HID++ 2.0."""

    def __init__(self, hidraw_path, device_index):
        self.hidraw_path = hidraw_path
        self.device_index = device_index
        self.fd = None
        self._feature_cache = {}  # feature_id -> feature_index

    def open(self):
        self.fd = os.open(self.hidraw_path, os.O_RDWR | os.O_NONBLOCK)
        # Drain
        while select.select([self.fd], [], [], 0.05)[0]:
            os.read(self.fd, 64)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _send(self, report_id, feature_index, func_id, *params):
        """Send an HID++ request and return the response."""
        sw_id = 0x0A
        header = (func_id << 4) | sw_id

        if report_id == HIDPP_SHORT:
            msg = bytes([report_id, self.device_index, feature_index, header])
            msg += bytes(params[:3])
            msg = msg.ljust(7, b'\x00')
        else:
            msg = bytes([report_id, self.device_index, feature_index, header])
            msg += bytes(params[:16])
            msg = msg.ljust(20, b'\x00')

        os.write(self.fd, msg)

        # Read response with timeout
        deadline = time.time() + 2.0
        while time.time() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if not r:
                continue
            resp = os.read(self.fd, 64)
            if len(resp) < 4:
                continue
            # Check if this is our response (matching feature_index and func)
            if resp[1] == self.device_index:
                if resp[2] == 0x8F:
                    # Error
                    err_code = resp[6] if len(resp) > 6 else 0xFF
                    raise HidppError(f"HID++ error: sub=0x{resp[3]:02X} addr=0x{resp[4]:02X} err=0x{err_code:02X}")
                if resp[2] == feature_index:
                    return resp
        raise TimeoutError("No HID++ response received")

    def get_feature_index(self, feature_id):
        """Get the feature index for a given feature ID using IRoot.GetFeature."""
        if feature_id in self._feature_cache:
            return self._feature_cache[feature_id]

        resp = self._send(HIDPP_LONG, 0x00, 0x00,
                          (feature_id >> 8) & 0xFF,
                          feature_id & 0xFF)
        idx = resp[4]
        if idx == 0:
            raise HidppError(f"Feature 0x{feature_id:04X} not available on device")
        self._feature_cache[feature_id] = idx
        return idx

    def set_smartshift(self, ratchet_mode):
        """Set the scroll wheel mode.

        ratchet_mode: True = ratchet (ticked), False = free-spin (unticked)

        Sets the SmartShift auto-disengage threshold to 255 (max) so the adaptive
        speed-based switching is effectively disabled — the wheel stays in whichever
        mode you set until you explicitly toggle it.
        """
        feat_idx = self.get_feature_index(FEAT_SMART_SHIFT)
        # SmartShift.SetConfig (function 1)
        # byte[0]: wheel mode — 0x01 = free-spin, 0x02 = ratchet
        # byte[1]: auto-disengage threshold (1-255)
        #          Higher = harder to trigger auto-switch. 255 = effectively never.
        mode_byte = 0x02 if ratchet_mode else 0x01
        self._send(HIDPP_LONG, feat_idx, 0x01, mode_byte, 0xFF)

    def get_smartshift(self):
        """Get current SmartShift configuration.

        Returns (is_ratcheted: bool, threshold: int)
        """
        feat_idx = self.get_feature_index(FEAT_SMART_SHIFT)
        resp = self._send(HIDPP_LONG, feat_idx, 0x00)
        # byte[0]: 0x01=free-spin, 0x02=ratchet
        # byte[1]: auto-disengage threshold (higher = harder to trigger)
        is_ratcheted = resp[4] == 0x02
        threshold = resp[5]
        return is_ratcheted, threshold

    def divert_button(self, cid, divert=True):
        """Divert a control ID to send HID++ notifications instead of normal HID events.

        cid: control ID from the REPROG_CONTROLS_V4 feature
        divert: True to divert to host, False to restore normal behavior
        """
        feat_idx = self.get_feature_index(FEAT_REPROG_CONTROLS_V4)

        # First, get the number of controls
        resp = self._send(HIDPP_LONG, feat_idx, 0x00)
        count = resp[4]

        # Find the control and set diversion
        for i in range(count):
            resp = self._send(HIDPP_LONG, feat_idx, 0x01, i)
            ctrl_cid = (resp[4] << 8) | resp[5]
            if ctrl_cid == cid:
                # SetControlReporting (function 3)
                flags = 0x03 if divert else 0x01  # bit 0=remap, bit 1=divert
                self._send(HIDPP_LONG, feat_idx, 0x03,
                           (cid >> 8) & 0xFF, cid & 0xFF,
                           flags, 0x00, 0x00)
                return True
        return False

    def read_notifications(self, timeout=0.01):
        """Read any pending HID++ notifications."""
        notifications = []
        while True:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                break
            try:
                resp = os.read(self.fd, 64)
                if len(resp) >= 4 and resp[1] == self.device_index:
                    notifications.append(resp)
            except OSError:
                break
        return notifications


class HidppError(Exception):
    pass


# --- Main Controller ---

class MXController:
    """Main controller that handles evdev remapping and HID++ scroll control."""

    def __init__(self, config):
        self.config = config
        self.mouse_dev = None
        self.uinput = None
        self.hidpp = None
        self.is_ratcheted = True  # Start in ratchet mode
        self.running = False

        # Parse button map: str keys to int
        self.button_map = {}
        for src, dst in config.get("button_map", {}).items():
            src_code = int(src)
            if isinstance(dst, str):
                dst_code = getattr(ecodes, dst, None)
                if dst_code is None:
                    print(f"Warning: unknown key code '{dst}', skipping")
                    continue
            else:
                dst_code = int(dst)
            self.button_map[src_code] = dst_code

        self.scroll_toggle_btn = config.get("scroll_toggle_button")
        if self.scroll_toggle_btn is not None:
            self.scroll_toggle_btn = int(self.scroll_toggle_btn)

        # Gesture state
        gb = config.get("gesture_button")
        self.gesture_btn = int(gb) if gb is not None else None
        self.gesture_threshold = int(config.get("gesture_threshold", 80))
        self.gestures = {}
        for direction, keys in (config.get("gestures") or {}).items():
            if not keys:
                continue
            resolved = []
            ok = True
            for k in keys:
                if isinstance(k, str):
                    code = getattr(ecodes, k, None)
                    if code is None:
                        print(f"Warning: unknown key '{k}' in gesture '{direction}', skipping")
                        ok = False
                        break
                    resolved.append(code)
                else:
                    resolved.append(int(k))
            if ok:
                self.gestures[direction] = resolved
        self._gesture_active = False
        self._gesture_dx = 0
        self._gesture_dy = 0
        self._gesture_fired = False

    def find_devices(self):
        """Discover and open required devices."""
        name_match = self.config.get("mouse_name_match", "Logitech USB Receiver Mouse")
        print(f"Looking for mouse matching: '{name_match}'...")

        self.mouse_dev = find_mouse_evdev(name_match)
        if not self.mouse_dev:
            print(f"Mouse not found. Available devices:")
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    print(f"  {path}: {dev.name}")
                    dev.close()
                except (PermissionError, OSError) as e:
                    print(f"  {path}: <permission denied>")
            return False

        print(f"Found mouse: {self.mouse_dev.name} at {self.mouse_dev.path}")

        # Find HID++ device for scroll mode control
        vid = self.config.get("hidraw_vendor_id", "046d")
        hidraw = find_hidraw_device(vid)
        if hidraw:
            print(f"Found HID++ device: {hidraw}")
            dev_idx = self.config.get("bolt_device_index")
            if dev_idx is None:
                print("Auto-detecting Bolt device index (move the mouse if it's asleep)...")
                dev_idx = find_bolt_device_index(hidraw)
            if dev_idx:
                print(f"Mouse is at Bolt device index: {dev_idx}")
                self.hidpp = HidppDevice(hidraw, dev_idx)
            else:
                print("Could not find mouse on Bolt receiver (it may be asleep).")
                print("Scroll mode toggle will not be available until the mouse wakes up.")
                print("Button remapping will still work.")
        else:
            print("HID++ device not found. Scroll mode toggle will not be available.")

        return True

    def setup_uinput(self):
        """Create a virtual input device that mirrors the mouse's capabilities plus extra keys."""
        caps = self.mouse_dev.capabilities()

        # Add any target keys from our button map that the original device doesn't have
        key_caps = set(caps.get(ecodes.EV_KEY, []))
        for target_code in self.button_map.values():
            key_caps.add(target_code)
        for chord in self.gestures.values():
            for code in chord:
                key_caps.add(code)

        # Add KEY_HOMEPAGE and common keys we might want to map to
        for extra in [ecodes.KEY_HOMEPAGE, ecodes.KEY_BACK, ecodes.KEY_FORWARD,
                      ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN, ecodes.KEY_MUTE,
                      ecodes.KEY_PLAYPAUSE, ecodes.KEY_NEXTSONG, ecodes.KEY_PREVIOUSSONG,
                      ecodes.KEY_COPY, ecodes.KEY_PASTE, ecodes.KEY_CUT, ecodes.KEY_UNDO]:
            key_caps.add(extra)

        new_caps = {}
        for ev_type, ev_codes in caps.items():
            if ev_type == ecodes.EV_KEY:
                new_caps[ev_type] = sorted(key_caps)
            elif ev_type == 0:  # EV_SYN - skip, auto-added
                continue
            else:
                new_caps[ev_type] = ev_codes

        self.uinput = UInput(new_caps, name="mxctl virtual mouse",
                             vendor=self.mouse_dev.info.vendor,
                             product=self.mouse_dev.info.product)
        print(f"Created virtual input device: {self.uinput.device.path}")

    def setup_hidpp(self):
        """Initialize HID++ connection and set initial scroll mode."""
        if not self.hidpp:
            return

        try:
            self.hidpp.open()

            # Disable adaptive SmartShift - set to pure ratchet with speed=1
            self.hidpp.set_smartshift(ratchet_mode=True)
            self.is_ratcheted = True
            print("Adaptive SmartShift disabled. Scroll wheel set to ratchet (ticked) mode.")

            # Divert the Smart Shift button so presses come to us as HID++ notifications
            # instead of being handled by firmware (which would re-enable adaptive mode)
            if self.config.get("divert_smartshift", True):
                try:
                    self._smartshift_feat_idx = self.hidpp.get_feature_index(FEAT_REPROG_CONTROLS_V4)
                    if self.hidpp.divert_button(CID_SMART_SHIFT, divert=True):
                        print("Smart Shift button diverted - press it to toggle ratchet/free-spin.")
                    else:
                        print("Warning: Could not find Smart Shift button for diversion.")
                except (HidppError, TimeoutError) as e:
                    print(f"Warning: Smart Shift diversion failed: {e}")

        except (HidppError, TimeoutError, OSError) as e:
            print(f"HID++ setup failed: {e}")
            print("Scroll mode toggle will not be available.")
            self.hidpp.close()
            self.hidpp = None

    def toggle_scroll_mode(self):
        """Toggle between ratchet and free-spin scroll modes."""
        if not self.hidpp:
            print("HID++ not available, cannot toggle scroll mode")
            return

        try:
            self.is_ratcheted = not self.is_ratcheted
            self.hidpp.set_smartshift(ratchet_mode=self.is_ratcheted)
            mode = "ratchet (ticked)" if self.is_ratcheted else "free-spin (smooth)"
            print(f"Scroll mode: {mode}")
        except (HidppError, TimeoutError, OSError) as e:
            print(f"Failed to toggle scroll mode: {e}")

    def run(self):
        """Main event loop."""
        self.running = True

        # Grab the mouse device exclusively
        self.mouse_dev.grab()
        print("Grabbed mouse device exclusively.")
        print(f"Button remapping active: {self.button_map}")
        if self.scroll_toggle_btn:
            print(f"Scroll toggle button: {self.scroll_toggle_btn}")
        print("Press Ctrl+C to stop.")
        print()

        try:
            # Build list of fds to poll
            poll_fds = [self.mouse_dev.fd]
            if self.hidpp and self.hidpp.fd is not None:
                poll_fds.append(self.hidpp.fd)

            while self.running:
                r, _, _ = select.select(poll_fds, [], [], 0.1)
                if not r:
                    continue

                for fd in r:
                    if fd == self.mouse_dev.fd:
                        for event in self.mouse_dev.read():
                            self._handle_event(event)
                    elif self.hidpp and fd == self.hidpp.fd:
                        for notif in self.hidpp.read_notifications():
                            self._handle_hidpp_notification(notif)

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.stop()

    def _handle_event(self, event):
        """Process an input event, applying remapping and gestures."""
        # Gesture: suppress motion while the gesture button is held
        if self._gesture_active and event.type == ecodes.EV_REL:
            if event.code == ecodes.REL_X:
                self._gesture_dx += event.value
                self._maybe_fire_gesture()
                return
            if event.code == ecodes.REL_Y:
                self._gesture_dy += event.value
                self._maybe_fire_gesture()
                return

        if event.type == ecodes.EV_KEY:
            # Scroll toggle button
            if self.scroll_toggle_btn and event.code == self.scroll_toggle_btn:
                if event.value == 1:
                    self.toggle_scroll_mode()
                return

            # Gesture button: enter/exit gesture mode
            if self.gesture_btn is not None and event.code == self.gesture_btn and self.gestures:
                if event.value == 1:  # press
                    self._gesture_active = True
                    self._gesture_dx = 0
                    self._gesture_dy = 0
                    self._gesture_fired = False
                    return  # don't emit the mapped key yet
                elif event.value == 0:  # release
                    self._gesture_active = False
                    if not self._gesture_fired:
                        # No gesture triggered → treat as a tap, fire mapped key
                        target = self.button_map.get(event.code)
                        if target is not None:
                            self.uinput.write(ecodes.EV_KEY, target, 1)
                            self.uinput.syn()
                            self.uinput.write(ecodes.EV_KEY, target, 0)
                            self.uinput.syn()
                    return

            # Regular button mapping
            if event.code in self.button_map:
                target = self.button_map[event.code]
                self.uinput.write(ecodes.EV_KEY, target, event.value)
                self.uinput.syn()
                return

        # Forward unmodified event
        self.uinput.write(event.type, event.code, event.value)

    def _maybe_fire_gesture(self):
        """Check if accumulated motion crosses the threshold and fire a gesture chord."""
        if self._gesture_fired:
            return
        adx = abs(self._gesture_dx)
        ady = abs(self._gesture_dy)
        if max(adx, ady) < self.gesture_threshold:
            return

        if adx >= ady:
            direction = "right" if self._gesture_dx > 0 else "left"
        else:
            direction = "down" if self._gesture_dy > 0 else "up"

        chord = self.gestures.get(direction)
        if not chord:
            return
        # Press the chord, then release in reverse order
        for code in chord:
            self.uinput.write(ecodes.EV_KEY, code, 1)
        self.uinput.syn()
        for code in reversed(chord):
            self.uinput.write(ecodes.EV_KEY, code, 0)
        self.uinput.syn()
        self._gesture_fired = True
        print(f"Gesture: {direction}")

    def _handle_hidpp_notification(self, data):
        """Handle an HID++ notification from the device."""
        if len(data) < 7:
            return

        feature_index = data[2]
        func_sw = data[3]
        func_id = (func_sw >> 4) & 0x0F

        # Check if this is a diverted button event from REPROG_CONTROLS_V4
        # Diverted button notifications come on the REPROG_CONTROLS_V4 feature index
        # with function 0 (divertedButtonEvent): bytes 4-5 = CID of pressed button
        reprog_idx = self.hidpp._feature_cache.get(FEAT_REPROG_CONTROLS_V4)
        if reprog_idx is not None and feature_index == reprog_idx and func_id == 0:
            cid = (data[4] << 8) | data[5]
            if cid == CID_SMART_SHIFT:
                self.toggle_scroll_mode()
            elif cid == 0x0000:
                pass  # Button released (CID=0 in release event)
            else:
                print(f"Diverted button CID=0x{cid:04X} (unhandled)")

    def stop(self):
        """Clean up and restore normal operation."""
        self.running = False
        if self.mouse_dev:
            try:
                self.mouse_dev.ungrab()
                print("Released mouse device.")
            except OSError:
                pass
        if self.uinput:
            self.uinput.close()
        if self.hidpp:
            try:
                # Un-divert Smart Shift button so firmware handles it again
                self.hidpp.divert_button(CID_SMART_SHIFT, divert=False)
            except (HidppError, TimeoutError, OSError):
                pass
            try:
                # Restore SmartShift to ratchet mode (no adaptive)
                self.hidpp.set_smartshift(ratchet_mode=True)
            except (HidppError, TimeoutError, OSError):
                pass
            self.hidpp.close()


# --- Identify Mode ---

def identify_buttons(config):
    """Interactive mode to identify which evdev codes your mouse buttons produce."""
    name_match = config.get("mouse_name_match", "Logitech USB Receiver Mouse")
    dev = find_mouse_evdev(name_match)
    if not dev:
        print(f"Mouse not found matching '{name_match}'")
        print("Available devices:")
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
                print(f"  {path}: {d.name}")
                d.close()
            except (PermissionError, OSError):
                print(f"  {path}: <permission denied>")
        return

    print(f"Found: {dev.name} at {dev.path}")
    print()
    print("Press mouse buttons to see their evdev codes.")
    print("Press Ctrl+C to stop.")
    print()

    try:
        for event in dev.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1:  # Key down only
                name = ecodes.BTN.get(event.code) or ecodes.KEY.get(event.code) or "unknown"
                if isinstance(name, list):
                    name = name[0]
                print(f"  Button code: {event.code} (0x{event.code:03X}) = {name}")
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        dev.close()


# --- Udev Rules ---

UDEV_RULE = """\
# mxctl - allow user access to Logitech HID++ devices and input devices
# Logitech Bolt receiver - HID++ access
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c548", MODE="0660", GROUP="input"
# Logitech Bolt receiver - evdev input access (usually already handled by default)
SUBSYSTEM=="input", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="c548", MODE="0660", GROUP="input"
"""

def install_udev():
    """Install udev rules for non-root access."""
    rule_path = "/etc/udev/rules.d/99-mxctl.rules"
    print(f"This will write to {rule_path}")
    print("Content:")
    print(UDEV_RULE)

    if os.geteuid() != 0:
        print("Error: must be run as root to install udev rules.")
        print(f"Run: sudo {sys.argv[0]} --install-udev")
        return

    with open(rule_path, "w") as f:
        f.write(UDEV_RULE)
    print(f"Wrote {rule_path}")
    os.system("udevadm control --reload-rules && udevadm trigger")
    print("Reloaded udev rules.")
    print()
    print("You also need to be in the 'input' group:")
    print("  sudo usermod -aG input $USER")
    print("Then log out and back in.")


# --- Entry Point ---

def main():
    parser = argparse.ArgumentParser(
        description="mxctl - Lightweight MX Master 3S controller for Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  sudo mxctl.py                  # Run with default config
  sudo mxctl.py --identify       # Identify button codes
  sudo mxctl.py --init-config    # Write default config file
  sudo mxctl.py --install-udev   # Install udev rules for non-root access

Button map format in config.json:
  "button_map": {
    "277": "KEY_LEFTMETA",    # Thumb button -> Super/Win
    "275": "KEY_BACK"         # Side button -> Back
  }

Common key names: KEY_LEFTMETA, KEY_HOME, KEY_HOMEPAGE, KEY_BACK, KEY_FORWARD,
  KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE, KEY_PLAYPAUSE, KEY_NEXTSONG
""")
    parser.add_argument("--identify", action="store_true",
                        help="Interactive mode: press buttons to see their codes")
    parser.add_argument("--init-config", action="store_true",
                        help="Write default config to ~/.config/mxctl/config.json")
    parser.add_argument("--install-udev", action="store_true",
                        help="Install udev rules for non-root access (requires root)")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to config file (default: ~/.config/mxctl/config.json)")
    parser.add_argument("--no-hidpp", action="store_true",
                        help="Disable HID++ (no scroll mode toggle)")
    parser.add_argument("--list-keys", action="store_true",
                        help="List all available key names for button mapping")

    args = parser.parse_args()

    if args.install_udev:
        install_udev()
        return

    if args.init_config:
        save_default_config()
        return

    if args.list_keys:
        print("Available key names for button_map targets:")
        for name in sorted(dir(ecodes)):
            if name.startswith("KEY_"):
                print(f"  {name} = {getattr(ecodes, name)}")
        return

    # Load config
    if args.config:
        with open(args.config) as f:
            config = json.load(f)
    else:
        config = load_config()

    if args.identify:
        identify_buttons(config)
        return

    # Main operation
    ctrl = MXController(config)

    # Signal handler for clean shutdown
    def sig_handler(signum, frame):
        ctrl.running = False
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)

    if not ctrl.find_devices():
        sys.exit(1)

    ctrl.setup_uinput()

    if not args.no_hidpp:
        ctrl.setup_hidpp()

    ctrl.run()


if __name__ == "__main__":
    main()
