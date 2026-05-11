# rtl8188eus-efuse-tool

Userspace CLI for reading and programming EFuse memory on Realtek RTL8188EUS USB WiFi dongles.

**Status**: WIP MVP (2026-05-11). Read/dump first, write to follow.

## Why this exists

- `flashrom` only supports SPI flash, not internal RTL EFuse
- Vendor drivers (`8188eu-dkms`) expose `/proc/net/8188eu/wlan_mon/efuse_map` as **read-only display**
- Manufacturer MP tools are Windows-only and not OSS
- **Niche**: clean CLI with `dump`/`decode`/`write`/`verify` like `flashrom`, but for I2C/EFuse and direct USB

## Architecture

```
[CLI argparse] → [pyusb device handle] → [Realtek vendor requests]
                                            ↓
                                   [REG_EFUSE_ACCESS=0x69 unlock]
                                            ↓
                                   [byte-by-byte EFUSE_CTRL reads]
                                            ↓
                                   [decode logical layout]
```

Bypasses kernel driver — requires `8188eu` or `rtl8xxxu` to be **unbound** during operation.

## Supported chips

- RTL8188EUS (`0bda:8179`) — primary
- RTL8188ETV (`0bda:0179`) — TODO test

## Usage (planned MVP γ scope)

```
rtl8188eus-efuse info                              # USB descriptor + chip rev
rtl8188eus-efuse dump > efuse.bin                  # raw 256 bytes hex
rtl8188eus-efuse decode efuse.bin                  # human-readable (MAC, vendor, TX cal)
rtl8188eus-efuse write efuse-modified.bin          # write back (with backup auto)
rtl8188eus-efuse verify efuse-modified.bin         # compare flash with file
```

## Source reverse engineering

Based on `gglluukk/rtl8188eus` v5.3.9 fork (kernel 6.x/7.0.x patches):
- `hal/rtl8188e/rtl8188e_hal_init.c` — `efuse_read_phymap_from_txpktbuf`
- `core/efuse/rtw_efuse.c` — generic EFuse access
- `include/rtl8188e_spec.h` — register definitions

Key registers:
- `REG_EFUSE_ACCESS = 0x00CF` — Vendor Lock (write `0x69` to unlock)
- `EFUSE_CTRL` — read/write control

## Build / dev

```fish
cd ~/Projekty/rtl8188eus-efuse-tool
python -m venv .venv
source .venv/bin/activate.fish
pip install -r requirements.txt
sudo python src/efuse_tool.py info     # needs CAP_SYS_RAWIO + unbound driver
```

## License

GPL-3.0 (zgodnie z source 8188eu z którego pochodzi RE)

## Author

Mateusz Wala (syriusm), 2026
