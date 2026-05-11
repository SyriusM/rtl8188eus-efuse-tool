# Installation

## Requirements

- Linux x86_64 (tested on CachyOS / Arch with kernel 7.0.5)
- Python 3.10+
- libusb 1.0 (system package)
- Root access (USB raw IO requires `CAP_SYS_RAWIO` or sudo)
- RTL8188EUS or RTL8188ETV USB WiFi dongle

## Arch / CachyOS

```fish
# System deps
sudo pacman -S python libusb

# Clone + venv
git clone <repo-url> ~/Projekty/rtl8188eus-efuse-tool
cd ~/Projekty/rtl8188eus-efuse-tool
python -m venv .venv
source .venv/bin/activate.fish    # or activate / activate.bash

# Python deps
pip install -r requirements.txt           # runtime: pyusb
pip install -r requirements-dev.txt       # development: + pytest

# Tests
.venv/bin/pytest tests/ -v
```

## Debian / Ubuntu

```bash
sudo apt install python3 python3-venv libusb-1.0-0
# ... same venv steps
```

## Verification

```fish
sudo .venv/bin/python src/efuse_tool.py info
```

Should show `0bda:8179 RTL8188EUS` or `0bda:0179 RTL8188ETV`.

## Driver coexistence

The tool talks to the chip via libusb directly. If the in-kernel `8188eu` or
`rtl8xxxu` driver has claimed the interface, the tool will auto-detach it
during `dump`/`write` operations. You don't need to manually `modprobe -r`,
but you can if you prefer total isolation:

```fish
sudo modprobe -r 8188eu rtl8xxxu
# ... operations ...
sudo modprobe 8188eu                       # restore
```

## Permissions: avoiding sudo

Add a udev rule to grant your user access (no more sudo for read commands):

```
# /etc/udev/rules.d/99-rtl8188eus.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="8179", MODE="0660", GROUP="users"
```

Reload: `sudo udevadm control --reload-rules && sudo udevadm trigger`.

> **Note**: Even with udev rules, EFuse write (PowerSwitch, LDO control) may still require root because writing the high-voltage LDO registers is privileged on some kernels.
