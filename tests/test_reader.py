import os

import pytest

from lifeboat.device.image import MemoryDevice
from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
from lifeboat.errors import DeviceGoneError, DeviceHungError
from lifeboat.events import EventBus
from lifeboat.rescue import ReadMode, ReadPolicy, RescueReader, SectorMap, State

MiB = 1 << 20


def make(size=8 * MiB, plan=None, **policy):
    data = os.urandom(size)
    inner = MemoryDevice(data)
    dev = SimulatedFailingDevice(inner, plan or FaultPlan())
    defaults = dict(block_size=256 * 1024, timeout=0.2, unit=4096)
    defaults.update(policy)
    reader = RescueReader(dev, ReadPolicy(**defaults), events=EventBus())
    return data, dev, reader


def test_clean_read_returns_data_and_marks_good():
    data, dev, reader = make()
    out = reader.read(1000, 5000)
    assert out.complete
    assert bytes(out.data) == data[1000:6000]
    # reads are widened to whole sectors: [512, 6144)
    assert reader.map.state_at(0) == State.UNTRIED
    assert reader.map.state_at(512) == State.GOOD
    assert reader.map.state_at(6143) == State.GOOD
    assert reader.map.state_at(6144) == State.UNTRIED


def test_read_past_end_is_bad():
    data, dev, reader = make(size=MiB)
    out = reader.read(MiB - 512, 2048)
    assert bytes(out.data[:512]) == data[-512:]
    assert out.bad == [(MiB, MiB + 1536)]
    assert out.good == [(MiB - 512, MiB)]


def test_fast_pass_skips_and_never_retries_bad_area():
    bad = (3 * MiB + 8192, 3 * MiB + 12288)  # one 4 KiB physical sector
    data, dev, reader = make(plan=FaultPlan(bad=[bad]))
    out = reader.read(0, 8 * MiB, ReadMode.FAST)
    assert not out.complete
    assert not out.bad  # only "unread" in the fast pass
    total_unread = sum(e - s for s, e in out.unread)
    # the failing 256 KiB block plus at least one skip
    assert total_unread >= 256 * 1024 + 64 * 1024
    # readable data outside unread areas is correct
    for s, e in out.good:
        assert bytes(out.data[s:e]) == data[s:e]
    for s, e in out.unread:
        assert bytes(out.data[s:e]) == bytes(e - s)
    touches = dev.reads_touching(*bad)
    assert touches == 1
    # second fast read must not touch the device in failed/skipped areas
    out2 = reader.read(0, 8 * MiB, ReadMode.FAST)
    assert dev.reads_touching(*bad) == 1
    assert out2.unread == out.unread


def test_full_pass_sequence_isolates_single_bad_sector():
    bad = (3 * MiB + 8192, 3 * MiB + 12288)
    data, dev, reader = make(plan=FaultPlan(bad=[bad]))
    for mode in (ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE):
        out = reader.read(0, 8 * MiB, mode)
    assert out.bad == [bad]
    assert not out.unread
    expected = bytearray(data)
    expected[bad[0]:bad[1]] = bytes(bad[1] - bad[0])
    assert bytes(out.data) == bytes(expected)
    assert reader.map.ranges([State.BAD]) == [bad]
    # the bad sector was read only a handful of times in total
    assert dev.reads_touching(*bad) <= 4


def test_flaky_sector_recovered_by_retry():
    # Fails in the fast pass and in both trimming steps, then reads fine on retry.
    flaky = (MiB, MiB + 4096)
    data, dev, reader = make(size=2 * MiB, plan=FaultPlan(flaky={flaky: 3}))
    for mode in (ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE):
        out = reader.read(0, 2 * MiB, mode)
    assert out.bad == [flaky]
    out = reader.read(0, 2 * MiB, ReadMode.RETRY)
    assert out.complete
    assert bytes(out.data) == data
    assert reader.map.ranges([State.GOOD]) == [(0, 2 * MiB)]


def test_each_retry_pass_tries_bad_units_once():
    flaky = (MiB, MiB + 4096)
    data, dev, reader = make(size=2 * MiB, plan=FaultPlan(flaky={flaky: 4}))  # fast, split, edge, retry#1 fail
    for mode in (ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE):
        reader.read(0, 2 * MiB, mode)
    before = dev.reads_touching(*flaky)
    assert not reader.read(0, 2 * MiB, ReadMode.RETRY).complete
    assert dev.reads_touching(*flaky) == before + 1
    assert reader.read(0, 2 * MiB, ReadMode.RETRY).complete


def test_timeouts_escalate_to_hung():
    hang = (0, 8 * MiB)
    data, dev, reader = make(plan=FaultPlan(hang=[hang]), timeout=0.01, max_consecutive_timeouts=3,
                             skip_initial=4096, block_size=4096)
    with pytest.raises(DeviceHungError):
        for offset in range(0, 8 * MiB, 64 * 1024):
            reader.read(offset, 4096, ReadMode.SWEEP)


def test_disconnect_raises_gone_without_marking_failed():
    data, dev, reader = make(plan=FaultPlan(disconnect_after_reads=3), block_size=64 * 1024)
    with pytest.raises(DeviceGoneError):
        reader.read(0, 8 * MiB)
    failed = reader.map.ranges([State.FAILED, State.BAD])
    assert failed == []
    good = reader.map.ranges([State.GOOD])
    assert good == [(0, 3 * 64 * 1024)]
    dev.reconnect()
    out = reader.read(0, 8 * MiB)
    assert out.complete
    assert bytes(out.data) == data


def test_slow_area_triggers_skip_in_fast_mode():
    slow = (2 * MiB, 2 * MiB + 4096)
    data, dev, reader = make(plan=FaultPlan(slow=[slow], slow_delay=0.05), slow_seconds=0.02, timeout=1.0)
    out = reader.read(0, 8 * MiB, ReadMode.FAST)
    # the slow block itself was read fine
    assert reader.map.state_at(2 * MiB) == State.GOOD
    assert reader.stats.slow_reads == 1
    # the area right after it was skipped for now
    assert reader.map.state_at(2 * MiB + 256 * 1024) == State.SKIPPED
    out = reader.read(0, 8 * MiB, ReadMode.SWEEP)
    assert out.complete
    assert bytes(out.data) == data


def test_existing_map_prevents_rereads(tmp_path):
    data = os.urandom(MiB)
    inner = MemoryDevice(data)
    plan = FaultPlan(bad=[(0, MiB)])
    dev = SimulatedFailingDevice(inner, plan)
    sm = SectorMap(MiB)
    sm.set(0, MiB, State.BAD)
    reader = RescueReader(dev, ReadPolicy(timeout=0.1), sector_map=sm)
    out = reader.read(0, MiB, ReadMode.SCRAPE)
    assert out.bad == [(0, MiB)]
    assert dev.reads == 0


def test_cache_serves_repeated_metadata_reads():
    data, dev, reader = make()
    reader.read(4096, 8192)
    reads = dev.reads
    out = reader.read(4096 + 512, 4096)
    assert bytes(out.data) == data[4096 + 512:4096 + 512 + 4096]
    assert dev.reads == reads


def test_read_critical_escalates():
    bad = (8192, 12288)
    data, dev, reader = make(size=MiB, plan=FaultPlan(bad=[bad]))
    out = reader.read_critical(0, 16384)
    assert out.bad == [bad]
    assert bytes(out.data[:8192]) == data[:8192]
    assert bytes(out.data[12288:16384]) == data[12288:16384]
