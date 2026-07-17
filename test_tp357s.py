#!/usr/bin/env python3
"""Unit tests for the pure TP357S protocol helpers in tp357tool.py.

Wire-format expectations (framing, checksums, length field endianness) are
pinned against a real capture from a TP357S device (see tp357s_decode's
docstring). Run with: .venv/bin/python -m pytest test_tp357s.py
"""

import datetime
import math

import pytest

from tp357tool import (tp357s_datetime_cmd, tp357s_history_cmds,
                       tp357s_decode, tp357s_hourly)

NOW = datetime.datetime(2026, 7, 17, 20, 15, 42)  # a Friday


def mkstream(records):
    """Build a valid history stream for a list of (temp, humid) records."""
    payload = b"".join(
        int(round(t * 10)).to_bytes(2, "little", signed=True) + bytes([h])
        for t, h in records)
    inner = b"\x01" + (len(payload) + 1).to_bytes(3, "little") + b"\x00" + payload
    return b"\xcc\xcc" + inner + bytes([sum(inner) & 0xff]) + b"\x66\x66"


def test_datetime_cmd():
    cmd = tp357s_datetime_cmd(NOW)
    assert cmd[:-1] == bytes([0xa5, 26, 7, 17, 20, 15, 42, 5])  # Fri = 5
    assert cmd[-1] == sum(cmd[:-1]) & 0xff


def test_history_cmds():
    cmd1, cmd2, cmd3 = tp357s_history_cmds(NOW, 28800)
    assert cmd1 == bytes.fromhex("cccc0201000001046666")
    assert cmd2 == bytes.fromhex("cccc04000000046666")
    assert cmd3[:2] == b"\xcc\xcc" and cmd3[-2:] == b"\x66\x66"
    body = cmd3[2:-3]
    assert body[:5] == bytes([0x01, 0x09, 0, 0, 0])
    assert body[5:11] == bytes([26, 7, 17, 20, 15, 42])
    assert int.from_bytes(body[11:13], "little") == 28800
    assert cmd3[-3] == sum(body) & 0xff


def test_decode_real_capture():
    # Prefix of an actual 100-record response captured from the device
    # (re-framed to 3 records with a recomputed length/checksum).
    stream = mkstream([(25.5, 0x39)] * 3)
    assert stream[:2 + 1 + 3 + 1 + 3] == bytes.fromhex("cccc010a000000ff0039")
    assert tp357s_decode(stream) == [(25.5, 57)] * 3


def test_decode_signed_temp_and_order():
    stream = mkstream([(-1.0, 40), (26.2, 57)])
    assert tp357s_decode(stream) == [(-1.0, 40), (26.2, 57)]  # newest first


def test_decode_empty():
    assert tp357s_decode(mkstream([])) == []


def test_decode_bad_framing():
    with pytest.raises(ValueError, match="framing"):
        tp357s_decode(b"\xcc\xcc\x01\x00")
    with pytest.raises(ValueError, match="framing"):
        tp357s_decode(b"\xc2" + mkstream([(20.0, 50)]))


def test_decode_dropped_chunk():
    stream = mkstream([(20.0, 50)] * 10)
    # losing an interior chunk shortens the payload vs the declared length
    with pytest.raises(ValueError, match="length mismatch"):
        tp357s_decode(stream[:10] + stream[22:])


def test_decode_corruption():
    stream = bytearray(mkstream([(20.0, 50)] * 4))
    stream[8] ^= 0x01  # flip a bit in a temperature byte
    with pytest.raises(ValueError, match="checksum"):
        tp357s_decode(bytes(stream))


def test_hourly_alignment():
    # Fetch at 20:15; 20 minute-records reach back across the 20:00 boundary.
    fetch_epoch = int(NOW.timestamp())
    records = [(20.0 + i, 50) for i in range(20)]  # newest first
    temps, humids = tp357s_hourly(records, fetch_epoch)
    assert len(temps) == 2
    # Newest 16 records (20:15..20:00) average into the last (current) hour,
    # the remaining 4 (19:59..19:56) into the previous one.
    assert temps[1] == round(sum(20.0 + i for i in range(16)) / 16, 1)
    assert temps[0] == round(sum(20.0 + i for i in range(16, 20)) / 4, 1)
    assert humids == [50, 50]


def test_hourly_single_hour():
    temps, humids = tp357s_hourly([(21.5, 60)], int(NOW.timestamp()))
    assert temps == [21.5] and humids == [60]
    assert not any(math.isnan(t) for t in temps)
