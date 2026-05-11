#!/usr/bin/env python3
"""
rtl8188eus-efuse-tool — userspace CLI for EFuse access on RTL8188EUS

WIP. MVP scope: info, dump, decode. Write to follow.
"""

import argparse
import json
import os
import sys
import struct
from datetime import datetime
from pathlib import Path

try:
    import usb.core
    import usb.util
except ImportError:
    print("ERROR: pyusb not installed. Run: pip install pyusb", file=sys.stderr)
    sys.exit(1)


VENDOR_ID = 0x0BDA
PRODUCT_IDS = {
    0x8179: "RTL8188EUS",
    0x0179: "RTL8188ETV",
}

REG_EFUSE_ACCESS = 0x00CF
EFUSE_ACCESS_ON = 0x69
EFUSE_ACCESS_OFF = 0x00

EFUSE_CTRL = 0x0030
EFUSE_TEST = 0x0034

REG_SYS_FUNC_EN = 0x0002
REG_SYS_CLKR = 0x0008
FEN_ELDR = 0x1000          # bit 12: EFuse Loader enable
LOADER_CLK_EN = 0x2000     # bit 13: EFuse loader clock
ANA8M = 0x0002             # bit 1: 8M analog clock
VOLTAGE_V25 = 0x03         # 2.5V LDO level (non-I-cut)

# OTP write rule: cells start at 0xFF, can only burn 1->0.
# Each byte you "write" actually masks: target_byte = current_byte & desired_byte.
# Never write to programmed cells unless you want to OR them with new mask.
OTP_INITIAL_VALUE = 0xFF

# Operational log — JSONL audit trail in repo root
OPS_LOG_PATH = Path(__file__).resolve().parent.parent / "ops_log.jsonl"


def log_op(operation, status="ok", **details):
    """Append one JSONL audit entry for any operation."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": operation,
        "status": status,
        "pid": os.getpid(),
        **details,
    }
    try:
        OPS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OPS_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as e:
        # Logging failure should never block tool operation
        print(f"WARN: ops_log write failed: {e}", file=sys.stderr)

EFUSE_PHYSICAL_LEN = 256   # raw EFuse cells (REAL_CONTENT_LEN_88E)
EFUSE_LOGICAL_LEN = 512    # decompressed logical map (MAP_LEN_88E)
EFUSE_MAX_SECTION = 64
EFUSE_MAX_WORD_UNIT = 4
EFUSE_DECODE = {
    0x00: ("Chip signature/header", 2),
    0x10: ("TX power calibration 2.4 GHz (ch1-12)", 12),
    0xD0: ("USB Vendor ID (LE)", 2),
    0xD2: ("USB Product ID (LE)", 2),
    0xD4: ("bcdDevice", 2),
    0xD7: ("MAC address", 6),
    0xDD: ("Mfr descriptor (len+type)", 2),
    0xDF: ("Manufacturer string", 7),
    0xE8: ("Product descriptor (type)", 1),
    0xE9: ("Product string", 11),
}


def find_device():
    """Locate first attached RTL8188EUS-family dongle."""
    for pid in PRODUCT_IDS:
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=pid)
        if dev:
            return dev, pid
    return None, None


def detach_kernel_driver(dev):
    """Unbind in-kernel driver if attached (8188eu / rtl8xxxu)."""
    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)


def usb_read_register(dev, addr, length=1):
    """Realtek USB vendor request: read N bytes from chip register."""
    return dev.ctrl_transfer(
        bmRequestType=0xC0,
        bRequest=0x05,
        wValue=addr,
        wIndex=0,
        data_or_wLength=length,
        timeout=1000,
    )


def usb_write_register(dev, addr, data):
    """Realtek USB vendor request: write N bytes to chip register."""
    if isinstance(data, int):
        data = bytes([data])
    return dev.ctrl_transfer(
        bmRequestType=0x40,
        bRequest=0x05,
        wValue=addr,
        wIndex=0,
        data_or_wLength=data,
        timeout=1000,
    )


def efuse_unlock(dev):
    usb_write_register(dev, REG_EFUSE_ACCESS, EFUSE_ACCESS_ON)


def efuse_lock(dev):
    usb_write_register(dev, REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)


def usb_read16(dev, addr):
    """Read 2 bytes from chip register (little-endian)."""
    data = usb_read_register(dev, addr, 2)
    return data[0] | (data[1] << 8)


def usb_write16(dev, addr, val):
    """Write 2 bytes to chip register (little-endian)."""
    usb_write_register(dev, addr, bytes([val & 0xFF, (val >> 8) & 0xFF]))


def usb_read32(dev, addr):
    """Read 4 bytes from chip register (little-endian)."""
    data = usb_read_register(dev, addr, 4)
    return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)


def usb_write32(dev, addr, val):
    """Write 4 bytes to chip register (little-endian)."""
    usb_write_register(dev, addr, bytes([
        val & 0xFF, (val >> 8) & 0xFF,
        (val >> 16) & 0xFF, (val >> 24) & 0xFF,
    ]))


def efuse_power_switch(dev, b_write, pwr_on):
    """
    RTL8188E EFuse power switch (port from hal_EfusePowerSwitch_RTL8188E).
    b_write=True for write operations (enables LDO 2.5V).
    pwr_on=True to power on, False to power off.
    """
    if pwr_on:
        # Unlock EFuse access
        usb_write_register(dev, REG_EFUSE_ACCESS, EFUSE_ACCESS_ON)
        # Reset: ensure FEN_ELDR set
        v = usb_read16(dev, REG_SYS_FUNC_EN)
        if not (v & FEN_ELDR):
            usb_write16(dev, REG_SYS_FUNC_EN, v | FEN_ELDR)
        # Clock: ensure LOADER_CLK_EN + ANA8M
        v = usb_read16(dev, REG_SYS_CLKR)
        if (not (v & LOADER_CLK_EN)) or (not (v & ANA8M)):
            usb_write16(dev, REG_SYS_CLKR, v | LOADER_CLK_EN | ANA8M)
        if b_write:
            # Enable LDO 2.5V (EFUSE_TEST+3 reg)
            t = usb_read_register(dev, EFUSE_TEST + 3, 1)[0]
            t = (t & 0x0F) | (VOLTAGE_V25 << 4) | 0x80
            usb_write_register(dev, EFUSE_TEST + 3, t)
    else:
        # Lock EFuse access
        usb_write_register(dev, REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)
        if b_write:
            # Disable LDO 2.5V
            t = usb_read_register(dev, EFUSE_TEST + 3, 1)[0]
            usb_write_register(dev, EFUSE_TEST + 3, t & 0x7F)


def efuse_one_byte_write(dev, addr, data):
    """
    Burn one byte at physical EFuse address (cell-level, 1->0 only).
    Port of efuse_OneByteWrite (rtw_efuse.c).
    Returns True on success, False on timeout.
    """
    efuse_power_switch(dev, b_write=True, pwr_on=True)
    try:
        ctrl = usb_read32(dev, EFUSE_CTRL)
        ctrl |= (1 << 21) | (1 << 31)           # program enable + start
        ctrl &= ~0x3FFFF                          # clear addr+data field
        ctrl |= ((addr << 8) | data) & 0x3FFFF    # addr[17:8] | data[7:0]
        usb_write32(dev, EFUSE_CTRL, ctrl)
        # Wait for BIT31 (CTRL+3 MSB) to auto-clear (write done)
        for _ in range(100):
            status = usb_read_register(dev, EFUSE_CTRL + 3, 1)[0]
            if not (status & 0x80):
                return True
        return False
    finally:
        efuse_power_switch(dev, b_write=True, pwr_on=False)


def efuse_find_empty(dev):
    """
    Return next write-pointer position by SIMULATING the PG packet parser.
    Walks headers + data following the same rules as the kernel driver.
    Parser stops at the first 0xFF in header position — that's our address.

    A 0xFF byte that lies in a DATA slot is NOT the parser stop; we have
    to follow header semantics to skip data bytes correctly. Likewise,
    bytes at high physical addresses (e.g. 0xFE-0xFF special markers)
    are unreachable by the parser if a 0xFF header occurred earlier.
    """
    efuse_unlock(dev)
    try:
        addr = 0
        while addr < EFUSE_PHYSICAL_LEN:
            header = efuse_read_byte(dev, addr)
            if header == 0xFF:
                return addr  # parser stops here = next write pointer
            addr += 1

            # Extended header? consume second byte
            if (header & 0x1F) == 0x0F:
                if addr >= EFUSE_PHYSICAL_LEN:
                    return addr
                ext = efuse_read_byte(dev, addr)
                addr += 1
                if (ext & 0x0F) == 0x0F:
                    continue  # bad ext header; skip
                wren = ext & 0x0F
            else:
                wren = header & 0x0F

            # Skip data bytes (2 bytes per "word used", bit clear in wren)
            for word_idx in range(EFUSE_MAX_WORD_UNIT):
                if not (wren & 0x01):
                    addr += 2
                    if addr >= EFUSE_PHYSICAL_LEN:
                        return EFUSE_PHYSICAL_LEN
                wren >>= 1
        return addr  # ran off end without finding 0xFF — truly full
    finally:
        efuse_lock(dev)


def efuse_read_byte(dev, offset):
    """Read one byte from EFuse at logical offset (0..255)."""
    # Write address to EFUSE_CTRL[23:8], set read bit at [31]
    addr_low = offset & 0xFF
    addr_high = (offset >> 8) & 0xFF
    usb_write_register(dev, EFUSE_CTRL + 1, addr_low)
    usb_write_register(dev, EFUSE_CTRL + 2, addr_high)
    ctrl = usb_read_register(dev, EFUSE_CTRL + 3, 1)[0]
    ctrl &= 0x7F  # clear write enable bit
    usb_write_register(dev, EFUSE_CTRL + 3, ctrl)
    # Set read bit
    ctrl |= 0x80
    usb_write_register(dev, EFUSE_CTRL + 3, ctrl)
    # Wait for read to finish (bit auto-clears)
    for _ in range(100):
        ctrl = usb_read_register(dev, EFUSE_CTRL + 3, 1)[0]
        if not (ctrl & 0x80):
            break
    # Read data byte
    data = usb_read_register(dev, EFUSE_CTRL, 1)[0]
    return data


def efuse_dump_physical(dev, size=EFUSE_PHYSICAL_LEN):
    """Read raw physical EFuse memory (cells, with PG headers)."""
    efuse_unlock(dev)
    try:
        data = bytearray(size)
        for offset in range(size):
            data[offset] = efuse_read_byte(dev, offset)
        return bytes(data)
    finally:
        efuse_lock(dev)


def physical_to_logical(raw):
    """
    Decompress physical EFuse (PG packets) into 512-byte logical map.

    Algorithm port from rtl8188eus gglluukk fork:
      hal/rtl8188e/rtl8188e_hal_init.c::Hal_EfuseReadEFuse88E

    Each PG packet = header (1 or 2 bytes) + up to 4 words (8 bytes) data.
    Header bits:
      - if (header & 0x1F) == 0x0F: extended (2-byte) header
        - u1temp = (header & 0xE0) >> 5    # high 3 bits of section index
        - next byte: offset = ((b & 0xF0) >> 1) | u1temp ; wren = b & 0x0F
      - else: offset = (header >> 4) & 0x0F ; wren = header & 0x0F
    wren bit clear (==0) means word IS used (present in packet).
    """
    logical = bytearray(b'\xFF' * EFUSE_LOGICAL_LEN)
    addr = 0
    while addr < len(raw):
        header = raw[addr]
        addr += 1
        if header == 0xFF:
            break  # end of programmed cells

        # Extended header?
        if (header & 0x1F) == 0x0F:
            u1temp = (header & 0xE0) >> 5
            if addr >= len(raw):
                break
            ext = raw[addr]
            addr += 1
            if (ext & 0x0F) == 0x0F:
                continue  # skip bad ext header
            offset = ((ext & 0xF0) >> 1) | u1temp
            wren = ext & 0x0F
        else:
            offset = (header >> 4) & 0x0F
            wren = header & 0x0F

        # Read up to 4 words
        for word_idx in range(EFUSE_MAX_WORD_UNIT):
            if not (wren & 0x01):  # bit clear = word IS used
                if addr + 2 > len(raw):
                    break
                low = raw[addr]
                high = raw[addr + 1]
                addr += 2
                if offset < EFUSE_MAX_SECTION:
                    logical[offset * 8 + word_idx * 2] = low
                    logical[offset * 8 + word_idx * 2 + 1] = high
            wren >>= 1
    return bytes(logical)


def efuse_dump(dev):
    """Read physical EFuse and decompress to logical 512-byte map."""
    raw = efuse_dump_physical(dev)
    return physical_to_logical(raw), raw


def cmd_info(args):
    dev, pid = find_device()
    if not dev:
        log_op("info", status="error", reason="no_device")
        print("ERROR: no RTL8188EUS-family device found.", file=sys.stderr)
        return 1
    chip = PRODUCT_IDS[pid]
    bus = dev.bus
    addr = dev.address
    serial = usb.util.get_string(dev, dev.iSerialNumber)
    print(f"Device:    {VENDOR_ID:04x}:{pid:04x} {chip}")
    print(f"Location:  bus {bus}, addr {addr}")
    print(f"Manufacturer: {usb.util.get_string(dev, dev.iManufacturer)}")
    print(f"Product:      {usb.util.get_string(dev, dev.iProduct)}")
    print(f"Serial:       {serial}")
    print(f"bcdDevice:    0x{dev.bcdDevice:04x}")
    log_op("info", chip=chip, vid=f"0x{VENDOR_ID:04X}", pid=f"0x{pid:04X}",
           bus=bus, addr=addr, serial=serial)
    return 0


def cmd_dump(args):
    dev, pid = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)
    if args.raw:
        print(f"Dumping {EFUSE_PHYSICAL_LEN} bytes physical EFuse...", file=sys.stderr)
        data = efuse_dump_physical(dev)
    else:
        print(f"Dumping + decompressing to {EFUSE_LOGICAL_LEN}-byte logical map...", file=sys.stderr)
        data, raw = efuse_dump(dev)
        if args.also_raw:
            raw_path = args.output + ".raw" if args.output else None
            if raw_path:
                with open(raw_path, "wb") as f:
                    f.write(raw)
                print(f"Also saved {len(raw)} bytes physical to {raw_path}", file=sys.stderr)
    if args.output:
        with open(args.output, "wb") as f:
            f.write(data)
        print(f"Saved {len(data)} bytes to {args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(data)
    return 0


def cmd_decode(args):
    with open(args.file, "rb") as f:
        data = f.read()
    if len(data) < EFUSE_LOGICAL_LEN:
        print(f"WARNING: short file {len(data)} bytes (expected {EFUSE_LOGICAL_LEN})", file=sys.stderr)
    print(f"EFuse decode of {args.file} ({len(data)} bytes)\n")
    for offset, (label, length) in sorted(EFUSE_DECODE.items()):
        if offset + length > len(data):
            continue
        chunk = data[offset:offset + length]
        hex_repr = " ".join(f"{b:02X}" for b in chunk)
        decoded = ""
        if label == "USB Vendor ID (LE)":
            decoded = f" → 0x{int.from_bytes(chunk, 'little'):04X}"
            vendor_name = "Realtek" if int.from_bytes(chunk, 'little') == 0x0BDA else "?"
            decoded += f" ({vendor_name})"
        elif label == "USB Product ID (LE)":
            decoded = f" → 0x{int.from_bytes(chunk, 'little'):04X}"
            pid = int.from_bytes(chunk, 'little')
            decoded += f" ({PRODUCT_IDS.get(pid, '?')})"
        elif label == "MAC address":
            decoded = f" → {':'.join(f'{b:02x}' for b in chunk)}"
        elif "string" in label:
            try:
                decoded = f' → "{chunk.decode("ascii").rstrip(chr(0xFF) + chr(0))}"'
            except UnicodeDecodeError:
                decoded = " → (non-ASCII)"
        elif "TX power" in label:
            decoded = f" → ch1..ch12 power idx (0.25 dBm units)"
        print(f"  0x{offset:02X} ({length:2d}b) {label:35s}  {hex_repr}{decoded}")
    return 0


def cmd_hex(args):
    with open(args.file, "rb") as f:
        data = f.read()
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{offset:04x}  {hex_part:<48s}  {ascii_part}")
    return 0


def cmd_find_empty(args):
    dev, _ = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)
    addr = efuse_find_empty(dev)
    if addr >= EFUSE_PHYSICAL_LEN:
        print(f"EFuse FULL — no empty cells in physical map ({EFUSE_PHYSICAL_LEN}B)")
        return 2
    print(f"First empty cell (0xFF): physical addr 0x{addr:02X} ({addr})")
    print(f"Remaining empty bytes:   {EFUSE_PHYSICAL_LEN - addr}")
    return 0


SANITY_PROTECTED_OFFSETS = {
    # Critical physical regions in already-programmed EFuse (don't OR-burn over)
    0x00: "chip signature/header",
    0x01: "chip signature/header",
}


def sanity_check_burn(addr, current, value, force=False):
    """Return (ok: bool, message: str)."""
    if addr in SANITY_PROTECTED_OFFSETS and not force:
        return False, f"refusing burn at 0x{addr:02X}: protected ({SANITY_PROTECTED_OFFSETS[addr]}). Use --force to override."
    # Reject if 4+ bits would be burned in already-programmed cell without --force
    if current != OTP_INITIAL_VALUE:
        bits_to_burn = bin(current & ~value).count("1")
        if bits_to_burn >= 4 and not force:
            return False, (
                f"refusing burn at 0x{addr:02X}: cell already 0x{current:02X}, "
                f"burning would clear {bits_to_burn} bits → 0x{current & value:02X}. "
                f"Use --force if intentional."
            )
    return True, "ok"


def auto_backup_before_write(dev, label):
    """Force a backup dump before any destructive operation."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"backups/auto-{label}-{timestamp}.bin"
    raw = efuse_dump_physical(dev)
    with open(backup_path, "wb") as f:
        f.write(raw)
    print(f"  AUTO-BACKUP: physical EFuse → {backup_path}", file=sys.stderr)
    return backup_path


def post_burn_logical_verify(dev, addr_burned):
    """Re-read chip + decompress, report logical impact of the burn."""
    raw = efuse_dump_physical(dev)
    logical = physical_to_logical(raw)
    print(f"  POST-BURN logical view (sections affected by addr 0x{addr_burned:02X}):")
    # Find which sections could be affected by parser reading from this addr
    # (we already burnt a byte → parser will reinterpret, possibly touching new section)
    return logical, raw


def cmd_write_byte(args):
    """Burn one byte at physical EFuse address (cell-level)."""
    dev, _ = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)

    addr = int(args.addr, 0)
    value = int(args.value, 0)
    if not (0 <= addr < EFUSE_PHYSICAL_LEN):
        print(f"ERROR: address 0x{addr:X} out of range (0..0x{EFUSE_PHYSICAL_LEN - 1:X})", file=sys.stderr)
        return 1
    if not (0 <= value <= 0xFF):
        print(f"ERROR: value 0x{value:X} out of byte range", file=sys.stderr)
        return 1

    # Pre-read current cell
    efuse_unlock(dev)
    current = efuse_read_byte(dev, addr)
    efuse_lock(dev)

    # OTP semantics: result = current AND value (1->0 only)
    effective = current & value
    print(f"Physical addr 0x{addr:02X}:")
    print(f"  current:        0x{current:02X} ({current:08b})")
    print(f"  requested:      0x{value:02X} ({value:08b})")
    print(f"  effective (AND): 0x{effective:02X} ({effective:08b})")
    if current == effective:
        print("  → no bits to burn (NOOP); skipping.")
        return 0

    # Sanity checks
    ok, msg = sanity_check_burn(addr, current, value, force=args.force)
    if not ok:
        print(f"  ✗ SANITY: {msg}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("  DRY RUN: no actual burn performed.")
        return 0

    if not args.yes:
        ans = input("  CONFIRM burn? [y/N]: ").strip().lower()
        if ans != "y":
            print("  aborted by user.")
            return 0

    # Auto-backup BEFORE any burn
    backup_path = auto_backup_before_write(dev, f"prewrite-byte-0x{addr:02X}")

    log_op("write_byte_start", addr=f"0x{addr:02X}", current=f"0x{current:02X}",
           requested=f"0x{value:02X}", effective=f"0x{effective:02X}", backup=backup_path)
    print(f"  burning 0x{effective:02X} at 0x{addr:02X}...")
    ok = efuse_one_byte_write(dev, addr, effective)
    if not ok:
        log_op("write_byte_end", status="timeout", addr=f"0x{addr:02X}", backup=backup_path)
        print(f"  ERROR: write timeout (BIT31 not cleared). Backup at {backup_path}", file=sys.stderr)
        return 2

    # Verify byte-level
    efuse_unlock(dev)
    after = efuse_read_byte(dev, addr)
    efuse_lock(dev)
    print(f"  read-back:      0x{after:02X} ({after:08b})")
    if after != effective:
        print(f"  ✗ VERIFY FAILED: expected 0x{effective:02X}, got 0x{after:02X}", file=sys.stderr)
        print(f"  Backup at {backup_path}", file=sys.stderr)
        return 3
    print("  ✓ BYTE VERIFY OK")

    # Post-burn logical verify (re-dump full + decompress + compare)
    logical_after, raw_after = post_burn_logical_verify(dev, addr)
    logical_before = physical_to_logical(open(backup_path, "rb").read())
    logical_diffs = [(i, b, a) for i, (b, a) in enumerate(zip(logical_before, logical_after)) if b != a]
    if not logical_diffs:
        print("  ✓ LOGICAL UNCHANGED (parser interpretation neutral)")
    else:
        print(f"  ⚠ LOGICAL CHANGED at {len(logical_diffs)} offsets:")
        for off, b, a in logical_diffs[:10]:
            print(f"    0x{off:03X}: before=0x{b:02X} after=0x{a:02X}")

    # Save post-burn snapshot
    post_path = f"backups/postwrite-byte-0x{addr:02X}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bin"
    with open(post_path, "wb") as f:
        f.write(raw_after)
    print(f"  Post-burn snapshot saved: {post_path}")
    log_op("write_byte_end", status="ok", addr=f"0x{addr:02X}",
           burned=f"0x{effective:02X}", verified=True,
           logical_diffs=len(logical_diffs), backup=backup_path, post_snap=post_path)
    return 0


def build_pg_packet(section, words):
    """
    Build PG packet bytes for a section + word changes (incremental update).

    section: int 0-63 (logical section index)
    words: dict {word_idx (0-3): u16_value} — only words we want to write.

    Returns: bytes of (1-or-2-byte header) + (2 bytes per word in `words`, low-then-high).
    """
    if not words:
        return b""

    # Build wren: bit CLEAR = word IS used (present in this packet)
    wren = 0x0F
    for word_idx in words:
        wren &= ~(1 << word_idx)
    wren &= 0x0F

    packet = bytearray()

    # Header: standard for section 0..15 (and wren != 0x0F which collides with extended marker)
    standard_header = (section << 4) | wren
    if section <= 15 and (standard_header & 0x1F) != 0x0F:
        packet.append(standard_header)
    else:
        # Extended header (2 bytes) — matches gglluukk C source exactly:
        #   pg_header = ((offset & 0x07) << 5) | 0x0F;      byte1
        #   pg_header = ((offset & 0x78) << 1) | word_en;   byte2
        # Section bits split: low 3 in byte1[7:5], high 3 in byte2[6:4]
        packet.append(((section & 0x07) << 5) | 0x0F)
        packet.append(((section & 0x78) << 1) | wren)

    # Data bytes: each used word = 2 bytes (low, high) little-endian
    for word_idx in range(EFUSE_MAX_WORD_UNIT):
        if word_idx in words:
            packet.append(words[word_idx] & 0xFF)
            packet.append((words[word_idx] >> 8) & 0xFF)

    return bytes(packet)


def calculate_write_plan(current_logical, target_logical):
    """
    Compare current vs target logical maps, return list of (section, packet_bytes).
    Each word that differs becomes part of a packet for that section.
    Returns also: total cells needed, byte-level diffs for visibility.
    """
    section_words = {}  # section -> {word_idx: u16_value}
    byte_diffs = []
    for off in range(min(len(current_logical), len(target_logical))):
        if target_logical[off] != current_logical[off]:
            byte_diffs.append((off, current_logical[off], target_logical[off]))
            section = off // 8
            word = (off % 8) // 2
            if section >= EFUSE_MAX_SECTION:
                continue  # out of range, skip
            if section not in section_words:
                section_words[section] = {}
            # Build full target word (both bytes from target)
            wlow = target_logical[section * 8 + word * 2]
            whigh = target_logical[section * 8 + word * 2 + 1]
            section_words[section][word] = wlow | (whigh << 8)

    packets = []
    total_cells = 0
    for section in sorted(section_words.keys()):
        pkt = build_pg_packet(section, section_words[section])
        packets.append((section, section_words[section], pkt))
        total_cells += len(pkt)

    return packets, total_cells, byte_diffs


def cmd_write(args):
    """Write a target logical EFuse image by appending PG packets for diffs."""
    dev, _ = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)

    with open(args.file, "rb") as f:
        target = f.read()
    if len(target) != EFUSE_LOGICAL_LEN:
        print(f"ERROR: target file must be {EFUSE_LOGICAL_LEN} bytes, got {len(target)}", file=sys.stderr)
        return 1

    # Read current chip state
    current_raw = efuse_dump_physical(dev)
    current_logical = physical_to_logical(current_raw)

    # Plan
    packets, total_cells, byte_diffs = calculate_write_plan(current_logical, target)
    write_addr = efuse_find_empty(dev)

    print(f"WRITE PLAN:")
    print(f"  target file:    {args.file}")
    print(f"  byte diffs:     {len(byte_diffs)}")
    print(f"  sections:       {len(packets)}")
    print(f"  total cells:    {total_cells}")
    print(f"  write pointer:  0x{write_addr:02X}")
    print(f"  remaining free: {EFUSE_PHYSICAL_LEN - write_addr}")

    if not packets:
        print("  → no changes needed; chip already matches target.")
        return 0

    if write_addr + total_cells > EFUSE_PHYSICAL_LEN:
        print(f"  ✗ NOT ENOUGH SPACE: need {total_cells} cells, have {EFUSE_PHYSICAL_LEN - write_addr}", file=sys.stderr)
        return 3

    print(f"\nPACKETS:")
    for section, words, pkt in packets:
        hex_repr = " ".join(f"{b:02X}" for b in pkt)
        word_summary = ", ".join(f"w{wi}=0x{val:04X}" for wi, val in sorted(words.items()))
        print(f"  section 0x{section:02X} ({len(pkt)} cells): {word_summary}  →  {hex_repr}")

    if args.dry_run:
        print("\nDRY RUN: nothing written.")
        return 0

    if not args.yes:
        ans = input(f"\nBurn {total_cells} cells starting at 0x{write_addr:02X}? [y/N]: ").strip().lower()
        if ans != "y":
            print("aborted by user.")
            return 0

    backup_path = auto_backup_before_write(dev, "prewrite-file")

    # Sequential burn
    addr = write_addr
    for section, words, pkt in packets:
        print(f"  burning section 0x{section:02X}: ", end="", flush=True)
        for byte in pkt:
            ok = efuse_one_byte_write(dev, addr, byte)
            if not ok:
                print(f"\n  ✗ write timeout at 0x{addr:02X}. Backup: {backup_path}", file=sys.stderr)
                return 4
            print(f"0x{addr:02X}=0x{byte:02X} ", end="", flush=True)
            addr += 1
        print()

    # Verify
    post_raw = efuse_dump_physical(dev)
    post_logical = physical_to_logical(post_raw)
    diffs = [(i, target[i], post_logical[i]) for i in range(EFUSE_LOGICAL_LEN) if target[i] != post_logical[i]]
    if not diffs:
        print(f"\n  ✓ LOGICAL MATCHES TARGET ({EFUSE_LOGICAL_LEN} bytes verified)")
    else:
        print(f"\n  ✗ {len(diffs)} byte mismatches after burn:", file=sys.stderr)
        for off, t, p in diffs[:15]:
            print(f"    0x{off:03X}: target=0x{t:02X} chip=0x{p:02X}", file=sys.stderr)
        return 5

    post_path = f"backups/postwrite-file-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bin"
    with open(post_path, "wb") as f:
        f.write(post_raw)
    print(f"  Post-burn snapshot: {post_path}")
    log_op("write_file_end", status="ok", file=args.file,
           sections=len(packets), cells=total_cells,
           backup=backup_path, post_snap=post_path)
    return 0


def cmd_diff(args):
    """Compare a .bin file against current chip logical view."""
    dev, _ = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)
    with open(args.file, "rb") as f:
        target = f.read()
    if args.raw:
        chip = efuse_dump_physical(dev)
        label = "physical"
    else:
        chip, _ = efuse_dump(dev)
        label = "logical"
    n = min(len(target), len(chip))
    diffs = [(i, target[i], chip[i]) for i in range(n) if target[i] != chip[i]]
    print(f"DIFF ({label}, {n} bytes compared):")
    print(f"  file:  {args.file}")
    print(f"  chip:  current state ({len(diffs)} differences)")
    if not diffs:
        print("  ✓ IDENTICAL")
        return 0
    print(f"\n  offset | file | chip | OTP-burnable? (chip & file != chip)")
    for off, t, c in diffs[:30]:
        otp_ok = "✓" if (c & t) == t else "✗ (needs 0→1 transition)"
        print(f"  0x{off:03X}  |  {t:02X}  |  {c:02X}  | {otp_ok}")
    if len(diffs) > 30:
        print(f"  ... {len(diffs) - 30} more.")
    return 0 if not diffs else 1


def cmd_verify(args):
    """Compare a .bin file against current chip dump."""
    dev, _ = find_device()
    if not dev:
        print("ERROR: no device", file=sys.stderr)
        return 1
    detach_kernel_driver(dev)
    with open(args.file, "rb") as f:
        target = f.read()
    if args.raw:
        chip = efuse_dump_physical(dev)
        label = "physical"
    else:
        chip, _ = efuse_dump(dev)
        label = "logical"
    n = min(len(target), len(chip))
    diffs = [(i, target[i], chip[i]) for i in range(n) if target[i] != chip[i]]
    print(f"Compared {n} bytes ({label}). {len(diffs)} differences.")
    for off, t, c in diffs[:20]:
        print(f"  0x{off:03X}: file=0x{t:02X} chip=0x{c:02X}")
    if len(diffs) > 20:
        print(f"  ... {len(diffs) - 20} more.")
    return 0 if not diffs else 1


def main():
    parser = argparse.ArgumentParser(
        prog="rtl8188eus-efuse",
        description="Userspace EFuse access for RTL8188EUS/ETV USB WiFi dongles",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="show USB descriptor + chip info")
    p_info.set_defaults(func=cmd_info)

    p_dump = sub.add_parser("dump", help="EFuse dump (logical 512B by default, raw 256B with --raw)")
    p_dump.add_argument("-o", "--output", help="output file (.bin), default stdout")
    p_dump.add_argument("--raw", action="store_true", help="dump physical 256B (cells with PG packets)")
    p_dump.add_argument("--also-raw", action="store_true", help="also save physical as <output>.raw")
    p_dump.set_defaults(func=cmd_dump)

    p_decode = sub.add_parser("decode", help="decode EFuse layout from file")
    p_decode.add_argument("file", help="EFuse .bin file")
    p_decode.set_defaults(func=cmd_decode)

    p_hex = sub.add_parser("hex", help="hexdump view of EFuse .bin file")
    p_hex.add_argument("file", help="EFuse .bin file")
    p_hex.set_defaults(func=cmd_hex)

    p_empty = sub.add_parser("find-empty", help="locate first 0xFF cell in physical EFuse")
    p_empty.set_defaults(func=cmd_find_empty)

    p_write = sub.add_parser("write-byte", help="burn single byte at physical address (1->0 only, irreversible)")
    p_write.add_argument("--addr", required=True, help="physical address (e.g. 0x90)")
    p_write.add_argument("--value", required=True, help="byte value to burn (e.g. 0xAA)")
    p_write.add_argument("--yes", action="store_true", help="skip confirm prompt")
    p_write.add_argument("--dry-run", action="store_true", help="show what would be written, don't burn")
    p_write.add_argument("--force", action="store_true", help="allow writing to already-programmed cell")
    p_write.set_defaults(func=cmd_write_byte)

    p_verify = sub.add_parser("verify", help="compare .bin file against current chip state")
    p_verify.add_argument("file", help="EFuse .bin file")
    p_verify.add_argument("--raw", action="store_true", help="compare physical 256B instead of logical 512B")
    p_verify.set_defaults(func=cmd_verify)

    p_diff = sub.add_parser("diff", help="show byte-level differences between file and chip + OTP burnability")
    p_diff.add_argument("file", help="EFuse .bin file (target state)")
    p_diff.add_argument("--raw", action="store_true", help="compare physical 256B instead of logical 512B")
    p_diff.set_defaults(func=cmd_diff)

    p_write_file = sub.add_parser("write", help="write target logical .bin to chip (incremental PG packet append)")
    p_write_file.add_argument("file", help="target logical EFuse .bin (must be 512 bytes)")
    p_write_file.add_argument("--yes", action="store_true", help="skip confirm prompt")
    p_write_file.add_argument("--dry-run", action="store_true", help="show planned packets, don't burn")
    p_write_file.set_defaults(func=cmd_write)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
