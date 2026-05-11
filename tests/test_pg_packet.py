"""Tests for PG packet encoder."""
import pytest

# Skip if pyusb unavailable (CI environments)
pytest.importorskip("usb")

from efuse_tool import build_pg_packet


def test_no_words_returns_empty():
    assert build_pg_packet(0, {}) == b""


def test_standard_header_one_word():
    """Section 5, word 0 = 0x1234 → header 0x5E (offset=5, wren=0xE) + LE data."""
    pkt = build_pg_packet(5, {0: 0x1234})
    assert pkt == bytes([0x5E, 0x34, 0x12])


def test_standard_header_two_words():
    """Section 3, words 0+2 = 0xAABB, 0xCCDD → wren=0xA (1010)."""
    pkt = build_pg_packet(3, {0: 0xAABB, 2: 0xCCDD})
    # wren bits clear = used: word0 & word2 → wren = 0xF & ~(1) & ~(4) = 0xA
    # header = (3 << 4) | 0xA = 0x3A
    assert pkt[0] == 0x3A
    # Data: word0 (LE), word2 (LE) — word_idx iterates 0..3 in order
    assert pkt[1:3] == bytes([0xBB, 0xAA])
    assert pkt[3:5] == bytes([0xDD, 0xCC])


def test_standard_header_all_four_words():
    """Section 1, all 4 words → wren=0x0 (all used) → header 0x10."""
    pkt = build_pg_packet(1, {0: 0x1111, 1: 0x2222, 2: 0x3333, 3: 0x4444})
    assert pkt[0] == 0x10
    assert len(pkt) == 1 + 8  # 1 header + 4 words × 2 bytes


def test_extended_header_section_26_word0():
    """Section 26 (0b011010) word 0 = 0x9999 → extended header per C encoder.
    byte1 = ((26 & 0x07) << 5) | 0x0F = (2 << 5) | 0x0F = 0x4F
    byte2 = ((26 & 0x78) << 1) | 0x0E (wren=E, word 0 used) = (0x18 << 1) | 0x0E = 0x30 | 0x0E = 0x3E
    Matches Pentagram real chip @ physical offset 0x4F.
    """
    pkt = build_pg_packet(26, {0: 0x9999})
    assert pkt[0] == 0x4F
    assert pkt[1] == 0x3E
    assert pkt[2:4] == bytes([0x99, 0x99])


def test_extended_header_roundtrip_via_decoder():
    """Encode + decode should produce the same logical layout (no bit shuffle)."""
    from efuse_tool import physical_to_logical
    for section, word_val in [(16, 0xAAAA), (26, 0xBEEF), (31, 0xCAFE), (63, 0xDEAD)]:
        pkt = build_pg_packet(section, {0: word_val})
        raw = bytearray([0xFF] * 256)
        raw[:len(pkt)] = pkt
        logical = physical_to_logical(bytes(raw))
        base = section * 8
        assert logical[base + 0] == (word_val & 0xFF), (
            f"section {section} word 0 low byte mismatch: got 0x{logical[base + 0]:02X}, "
            f"expected 0x{word_val & 0xFF:02X}"
        )
        assert logical[base + 1] == ((word_val >> 8) & 0xFF), (
            f"section {section} word 0 high byte mismatch"
        )


def test_extended_header_section_above_15():
    """Sections > 15 must always use extended header."""
    pkt = build_pg_packet(16, {0: 0x1234})
    # byte1 should have (byte & 0x1F) == 0x0F
    assert (pkt[0] & 0x1F) == 0x0F


def test_standard_collision_with_extended_marker():
    """Section 0, wren=0x0F would collide with extended marker (0x0F) — must use extended."""
    # No way to get wren=0x0F with non-empty words dict (every word added clears a bit),
    # but for completeness ensure section 0 with all bits set in wren works.
    # Single word (e.g. word 0) → wren = 0xE = 1110 → header = 0x0E (no collision).
    pkt = build_pg_packet(0, {0: 0xFFFF})
    assert pkt[0] == 0x0E


def test_data_bytes_little_endian_order():
    pkt = build_pg_packet(2, {1: 0xABCD})
    # wren = 0xF & ~(1 << 1) = 0xD = 1101 → header (2 << 4) | 0xD = 0x2D
    assert pkt[0] == 0x2D
    # Data: word 1 low (0xCD), high (0xAB)
    assert pkt[1:3] == bytes([0xCD, 0xAB])
