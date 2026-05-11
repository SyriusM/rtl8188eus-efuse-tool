#!/usr/bin/env python3
"""
rtl8188eus-efuse-tool — userspace CLI for EFuse access on RTL8188EUS

WIP. MVP scope: info, dump, decode. Write to follow.
"""

import argparse
import sys
import struct
from datetime import datetime

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
        print("ERROR: no RTL8188EUS-family device found.", file=sys.stderr)
        return 1
    chip = PRODUCT_IDS[pid]
    bus = dev.bus
    addr = dev.address
    print(f"Device:    {VENDOR_ID:04x}:{pid:04x} {chip}")
    print(f"Location:  bus {bus}, addr {addr}")
    print(f"Manufacturer: {usb.util.get_string(dev, dev.iManufacturer)}")
    print(f"Product:      {usb.util.get_string(dev, dev.iProduct)}")
    print(f"Serial:       {usb.util.get_string(dev, dev.iSerialNumber)}")
    print(f"bcdDevice:    0x{dev.bcdDevice:04x}")
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
