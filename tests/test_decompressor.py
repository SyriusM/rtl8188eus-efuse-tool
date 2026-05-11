"""Tests for physical→logical EFuse decompressor."""
import pytest

pytest.importorskip("usb")

from efuse_tool import physical_to_logical, EFUSE_LOGICAL_LEN


def test_empty_physical_returns_all_ff():
    raw = bytes([0xFF] * 256)
    logical = physical_to_logical(raw)
    assert len(logical) == EFUSE_LOGICAL_LEN
    assert all(b == 0xFF for b in logical)


def test_single_standard_packet_section0_word0():
    """Header 0x0E (section 0, wren 0xE = word 0 used), data 0x29 0x81."""
    raw = bytearray([0xFF] * 256)
    raw[0] = 0x0E
    raw[1] = 0x29
    raw[2] = 0x81
    logical = physical_to_logical(bytes(raw))
    # Section 0 * 8 + word 0 * 2 = offset 0
    assert logical[0] == 0x29
    assert logical[1] == 0x81
    # Rest of section 0 untouched
    assert logical[2] == 0xFF
    assert logical[3] == 0xFF


def test_extended_packet_section_26():
    """Reproduce Pentagram's section 26 (USB descriptor) — matches real chip."""
    raw = bytearray([0xFF] * 256)
    # Real Pentagram chip uses byte1=0x4F (section bits[2:0]=2 in [7:5]) + byte2=0x30 (all words used, wren=0)
    # Confirmed against gold standard /proc/net/8188eu/wlan_mon/efuse_map
    raw[0] = 0x4F
    raw[1] = 0x30
    # All 4 words: vendor_lo, vendor_hi, product_lo, product_hi, bcd_lo, bcd_hi, mac0, mac1
    raw[2:10] = bytes([0xDA, 0x0B, 0x79, 0x81, 0x42, 0x66, 0x00, 0x24])
    logical = physical_to_logical(bytes(raw))
    base = 26 * 8
    assert logical[base + 0] == 0xDA   # vendor LE byte 0
    assert logical[base + 1] == 0x0B
    assert logical[base + 2] == 0x79   # product LE byte 0
    assert logical[base + 3] == 0x81
    assert logical[base + 6] == 0x00   # mac fragment
    assert logical[base + 7] == 0x24


def test_ff_header_stops_parsing():
    """0xFF byte at header position halts parser."""
    raw = bytearray([0xFF] * 256)
    raw[0] = 0x0E  # section 0 word 0
    raw[1] = 0xAA
    raw[2] = 0xBB
    raw[3] = 0xFF  # halt here
    # Pretend there's more data after — should be ignored
    raw[4] = 0x10  # would be section 1 all words
    raw[5:13] = bytes([0xCC] * 8)
    logical = physical_to_logical(bytes(raw))
    assert logical[0] == 0xAA
    assert logical[1] == 0xBB
    # Section 1 must remain unprogrammed
    assert logical[8] == 0xFF


def test_overwrite_word_with_later_packet():
    """Last write per word wins."""
    raw = bytearray([0xFF] * 256)
    # First packet: section 0 word 0 = 0x1111
    raw[0] = 0x0E
    raw[1] = 0x11
    raw[2] = 0x11
    # Second packet: section 0 word 0 = 0x2222
    raw[3] = 0x0E
    raw[4] = 0x22
    raw[5] = 0x22
    logical = physical_to_logical(bytes(raw))
    assert logical[0] == 0x22  # overwritten
    assert logical[1] == 0x22
