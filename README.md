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

## Usage examples

### Read-only

```fish
# Identify dongle
sudo .venv/bin/python src/efuse_tool.py info

# Dump logical 512-byte map (decompressed, like kernel /proc view)
sudo .venv/bin/python src/efuse_tool.py dump -o my-efuse.bin

# Dump physical 256-byte map (raw cells with PG packets)
sudo .venv/bin/python src/efuse_tool.py dump --raw -o my-efuse-raw.bin

# Human-readable decode (offline, works on .bin file)
.venv/bin/python src/efuse_tool.py decode my-efuse.bin

# Show first 0xFF in physical map (next write pointer)
sudo .venv/bin/python src/efuse_tool.py find-empty
```

### Comparison

```fish
# Verify a backup matches current chip
sudo .venv/bin/python src/efuse_tool.py verify my-efuse.bin

# Detailed per-byte diff with OTP burnability
sudo .venv/bin/python src/efuse_tool.py diff modified-target.bin
```

### Write (destructive, OTP — one-time programmable!)

```fish
# Single byte burn (POC; --dry-run shows plan, --yes skips confirm)
sudo .venv/bin/python src/efuse_tool.py write-byte --addr 0x8F --value 0xAA --dry-run
sudo .venv/bin/python src/efuse_tool.py write-byte --addr 0x8F --value 0xAA --yes

# Full file-based write (incremental PG packet append)
sudo .venv/bin/python src/efuse_tool.py write modified-target.bin --dry-run
sudo .venv/bin/python src/efuse_tool.py write modified-target.bin --yes
```

All destructive operations:
- auto-create a physical backup in `backups/auto-prewrite-*-<timestamp>.bin`
- print a clear plan before burning
- verify byte-level + logical-level after burn
- save a post-burn snapshot
- log to `ops_log.jsonl` (JSONL audit trail)

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

## Why testing matters here — a real story (2026-05-11)

During initial development, the test suite caught a **silent encoder bug** that
would have permanently bricked the target chip on the first real burn.

The bug: `build_pg_packet()` swapped the high and low 3 bits of the section
index when emitting the extended (2-byte) header. The Realtek encoder spec is:

```c
pg_header = ((offset & 0x07) << 5) | 0x0F;       // byte1: section[2:0] in [7:5]
pg_header = ((offset & 0x78) << 1) | word_en;    // byte2: section[6:3] in [7:4]
```

My initial Python port used `(section >> 3) & 0x07` for byte1 — wrong direction.
A `write` of MAC bytes targeting section 26 would have actually written to
section 27 — overwriting a *different* logical area in OTP cells, **irreversibly**.

The test `test_extended_packet_section_26` reproduces the Pentagram chip's
real physical header pattern (byte1=0x4F + byte2=0x30 for section 26 with
all words used). It failed on the first run, exposing the bug before any
real burn was attempted. Fix was a one-line swap; tests now green.

**Lesson**: bit-fiddling encoders for OTP / one-way memory require
roundtrip tests against known-good reference data. A 30-minute investment
in the suite prevented permanent hardware damage (and the related
"how do I explain this to myself" frustration).

## Recovery — what to do if chip stops enumerating after a burn

EFuse is **one-time programmable**. A botched burn can leave the dongle
unable to load firmware or report a corrupted USB descriptor. Steps:

### 1. Diagnose

```fish
lsusb | grep 0bda:8179               # is the device still on USB bus?
dmesg | tail -30                     # what does kernel say?
```

Common kernel messages and their meaning:

| Message | Cause |
|---|---|
| `Fatal - failed to parse EFuse` | EFuse content unparseable (corrupted PG packet sequence). |
| `LLT table init failed` | RF subsystem couldn't load calibration; usually transient (USB power glitches) but can be EFuse-related. |
| no `usb 1-X: New USB device` at all | Dongle not enumerating — could be power, cable, or `IS_VENDOR_8188E_I_CUT_SERIES` quirk after write. |

### 2. Try recovery

A botched packet can sometimes be "shadowed" by appending a corrective packet
with the same section index and the correct values. EFuse parser uses
last-write-wins per word, so:

```fish
# Identify the broken section
sudo .venv/bin/python src/efuse_tool.py dump -o broken-state.bin
sudo .venv/bin/python src/efuse_tool.py decode broken-state.bin

# Edit broken-state.bin in a hex editor to restore the original values
# (use your backup in backups/auto-prewrite-*.bin as reference!)
xxd backups/auto-prewrite-byte-0x8F-20260511-184958.bin | head

# Apply the corrected file
sudo .venv/bin/python src/efuse_tool.py write corrected.bin --dry-run
sudo .venv/bin/python src/efuse_tool.py write corrected.bin --yes
```

### 3. If recovery fails: physical limits

If the chip is bricked beyond software recovery:
- The chip is ~3 USD (8-15 PLN on Allegro). Treat as expended R&D budget.
- Keep the dead dongle for inspection / PCB salvage (antenna RP-SMA, USB connector).
- File a "Cmentarz" entry in your inventory (`~/lab/inventory/`).

### 4. Prevention checklist (before any `write` or `write-byte`)

- [ ] Have a fresh `backups/auto-prewrite-*.bin` from THIS session
- [ ] Reviewed `--dry-run` output for the exact operation
- [ ] Understand which bits will flip 1→0 and on which physical address
- [ ] Confirmed the burn target is in **fresh 0xFF cell range** (use `find-empty`)
- [ ] Logged operation justification in `ops_log.jsonl`

## Audit trail

Every operation (info/dump/write/verify) appends one JSONL entry to
`ops_log.jsonl` in the repo root. Format:

```json
{"ts":"2026-05-11T19:35:22","op":"write_byte_end","status":"ok","pid":12345,
 "addr":"0x8F","burned":"0xAA","verified":true,"logical_diffs":0,
 "backup":"backups/auto-prewrite-byte-0x8F-20260511-193512.bin",
 "post_snap":"backups/postwrite-byte-0x8F-20260511-193515.bin"}
```

For forensics: `jq '.' ops_log.jsonl | less`.

## License

GPL-3.0 (zgodnie z source 8188eu z którego pochodzi RE)

## Author

Mateusz Wala (syriusm), 2026
