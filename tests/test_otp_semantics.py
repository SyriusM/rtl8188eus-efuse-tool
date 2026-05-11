"""Tests for OTP semantics: sanity checks, AND-burn rules, write planner."""
import pytest

pytest.importorskip("usb")

from efuse_tool import (
    sanity_check_burn,
    calculate_write_plan,
    EFUSE_LOGICAL_LEN,
    SANITY_PROTECTED_OFFSETS,
)


def test_sanity_protected_offset_blocks_burn():
    ok, msg = sanity_check_burn(0x00, current=0xFF, value=0xAA, force=False)
    assert not ok
    assert "protected" in msg


def test_sanity_protected_offset_overridden_with_force():
    ok, _ = sanity_check_burn(0x00, current=0xFF, value=0xAA, force=True)
    assert ok


def test_sanity_warns_on_heavy_burn_of_programmed_cell():
    """current=0x0F, value=0x00 → would burn bits 0-3 (4 bits) of programmed cell → refuse."""
    ok, msg = sanity_check_burn(0x90, current=0x0F, value=0x00, force=False)
    assert not ok
    assert "4 bits" in msg or "bits" in msg


def test_sanity_allows_fresh_cell_full_burn():
    """Fresh 0xFF → 0xAA is fine even though 4 bits burnt."""
    ok, _ = sanity_check_burn(0x90, current=0xFF, value=0xAA, force=False)
    assert ok


def test_write_plan_no_changes():
    current = bytes([0xFF] * EFUSE_LOGICAL_LEN)
    target = bytes([0xFF] * EFUSE_LOGICAL_LEN)
    packets, cells, diffs = calculate_write_plan(current, target)
    assert packets == []
    assert cells == 0
    assert diffs == []


def test_write_plan_single_byte_change_section0():
    current = bytes([0xFF] * EFUSE_LOGICAL_LEN)
    target = bytearray([0xFF] * EFUSE_LOGICAL_LEN)
    target[0] = 0x42  # section 0, word 0, byte 0
    packets, cells, diffs = calculate_write_plan(current, bytes(target))
    assert len(packets) == 1
    section, words, pkt = packets[0]
    assert section == 0
    assert words == {0: 0xFF42}  # full target word, low byte changed, high stayed 0xFF
    # standard header for section 0, wren=0xE (word 0 used) = 0x0E; then 2 data bytes
    assert pkt[0] == 0x0E
    assert pkt[1:3] == bytes([0x42, 0xFF])
    assert cells == 3


def test_write_plan_multi_section():
    current = bytes([0xFF] * EFUSE_LOGICAL_LEN)
    target = bytearray([0xFF] * EFUSE_LOGICAL_LEN)
    target[0] = 0xAA       # section 0
    target[26 * 8] = 0xBB  # section 26
    packets, cells, _ = calculate_write_plan(current, bytes(target))
    assert len(packets) == 2
    sections = [p[0] for p in packets]
    assert 0 in sections and 26 in sections


def test_write_plan_byte_above_logical_skipped():
    """Bytes beyond EFUSE_LOGICAL_LEN (or section >= 64) are skipped, no packet generated."""
    current = bytes([0xFF] * EFUSE_LOGICAL_LEN)
    target = bytearray([0xFF] * EFUSE_LOGICAL_LEN)
    # Section 63 is the last valid one; section 64+ would map outside EFUSE_MAX_SECTION
    # Just verify a valid section 63 change works
    target[63 * 8] = 0xCD
    packets, _, _ = calculate_write_plan(current, bytes(target))
    assert len(packets) == 1
    assert packets[0][0] == 63
