# mxctl

Lightweight Linux controller for the Logitech MX Master 3S. No GUI, no bloat — just a single Python script that runs as a service.

**What it does:**

- **Remap any mouse button** to any keyboard key (e.g., thumb button to Super/Win key)
- **True ratchet/free-spin scroll toggle** — press the Smart Shift button to switch between ticked (ratchet) and smooth (free-spin) scrolling. No more adaptive SmartShift that auto-switches at a certain scroll velocity.

**Why not Solaar?**

Solaar doesn't support remapping buttons to arbitrary keyboard keys (only to other mouse functions). Its SmartShift control only lets you adjust the adaptive speed threshold — you can't fully disable the auto-switching behavior. mxctl solves both.

## Requirements

- Linux with kernel HID support
- Python 3.8+
- `evdev` Python package
- Logitech MX Master 3S connected via Bolt USB receiver

## Install

```bash
git clone https://github.com/RussellCastro/mxctl.git
cd mxctl
pip install evdev
```

## Quick start

```bash
# 1. Identify your mouse buttons
sudo python3 mxctl.py --identify

# 2. Run it
sudo python3 mxctl.py

# 3. (Optional) Write a config file to customize
python3 mxctl.py --init-config
# Edit ~/.config/mxctl/config.json
```

## Setup as a system service

```bash
# Install udev rules (allows the service to access HID devices)
sudo python3 mxctl.py --install-udev
sudo usermod -aG input $USER
# Log out and back in for group change to take effect

# Install and enable the systemd service
sudo cp mxctl.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mxctl

# Check status
systemctl status mxctl
```

## Removing Solaar (optional)

If you were using Solaar and want to replace it with mxctl:

```bash
sudo apt remove --purge solaar
```

## Configuration

Config lives at `~/.config/mxctl/config.json`. Generate the default with:

```bash
python3 mxctl.py --init-config
```

### Button mapping

Map any mouse button to any keyboard key. Use `--identify` to find button codes, and `--list-keys` to see all available key names.

```json
{
  "button_map": {
    "277": "KEY_LEFTMETA",
    "275": "KEY_BACK"
  }
}
```

**MX Master 3S button codes (via Bolt receiver):**

| Button | Code | Default evdev name |
|--------|------|--------------------|
| Thumb / Gesture | 277 | BTN_FORWARD |
| Back (side) | 275 | BTN_SIDE |
| Forward (side) | 276 | BTN_EXTRA |
| Middle click | 274 | BTN_MIDDLE |

The Smart Shift button (scroll wheel toggle) and DPI button don't produce evdev events — they're handled via HID++ diversion.

### Scroll wheel

By default, mxctl:
1. Disables the adaptive SmartShift (sets the auto-disengage threshold to max so it never triggers)
2. Diverts the Smart Shift button so pressing it toggles between pure ratchet and pure free-spin
3. Starts in ratchet (ticked) mode

### All config options

| Key | Default | Description |
|-----|---------|-------------|
| `mouse_name_match` | `"Logitech USB Receiver Mouse"` | Substring to match the evdev device name |
| `hidraw_vendor_id` | `"046d"` | Vendor ID for finding the HID++ hidraw device |
| `bolt_device_index` | `null` | Bolt receiver slot (1-6). `null` = auto-detect |
| `button_map` | `{"277": "KEY_LEFTMETA"}` | Button code -> key name mapping |
| `scroll_toggle_button` | `null` | Evdev button code to use as scroll toggle (alternative to HID++ diversion) |
| `divert_smartshift` | `true` | Divert the Smart Shift button via HID++ for scroll toggling |

## CLI reference

```
sudo python3 mxctl.py                  # Run the controller
sudo python3 mxctl.py --identify       # Identify button codes interactively
sudo python3 mxctl.py --install-udev   # Install udev rules
     python3 mxctl.py --init-config    # Write default config file
     python3 mxctl.py --list-keys      # List all available key names
     python3 mxctl.py --no-hidpp       # Run without HID++ (button remap only)
     python3 mxctl.py --config FILE    # Use a custom config file path
```

## How it works

- **Button remapping**: Grabs the mouse's evdev input device exclusively, creates a virtual input device via uinput, and forwards all events — substituting remapped button codes.
- **Scroll mode toggle**: Communicates with the mouse via the HID++ 2.0 protocol over the Bolt receiver's vendor-specific hidraw interface. Diverts the Smart Shift button (CID 0x00C4) so presses come to the host as HID++ notifications instead of being handled by firmware. On each press, sends a SmartShift SetConfig command to flip between ratchet (0x02) and free-spin (0x01) modes with threshold=255 to disable adaptive switching.

## Troubleshooting

**Mouse not found**: Make sure the mouse is awake (move it) and connected via the Bolt receiver. Check `sudo python3 mxctl.py --identify` to see available devices.

**Permission denied**: Run with `sudo`, or install udev rules with `--install-udev` and add yourself to the `input` group.

**Scroll toggle not working**: The mouse must be awake when mxctl starts for HID++ auto-detection to work. If HID++ fails, try setting `bolt_device_index` manually in the config (usually 1-6, check `solaar show` or try each).

**Mouse stops working after Ctrl+C**: mxctl should restore the device on clean exit. If it crashes, unplug and replug the Bolt receiver.

## License

MIT
