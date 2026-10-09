import random

import pytest

from lifeboat.rescue.sectormap import PARTIAL, MapFileError, SectorMap, State


def reference_states(size, ops):
    arr = [State.UNTRIED] * size
    for start, end, state in ops:
        for i in range(max(0, start), min(size, end)):
            arr[i] = state
    return arr


def map_states(sm):
    arr = []
    for start, end, state in sm.segments():
        arr.extend([state] * (end - start))
    return arr


def test_initial_state():
    sm = SectorMap(100)
    assert sm.segments() == [(0, 100, State.UNTRIED)]
    assert sm.totals()[State.UNTRIED] == 100


def test_set_and_merge():
    sm = SectorMap(100)
    sm.set(10, 20, State.GOOD)
    sm.set(20, 30, State.GOOD)
    assert sm.segments() == [(0, 10, State.UNTRIED), (10, 30, State.GOOD), (30, 100, State.UNTRIED)]
    sm.set(0, 10, State.GOOD)
    sm.set(30, 100, State.GOOD)
    assert sm.segments() == [(0, 100, State.GOOD)]
    assert sm.segment_count() == 1


def test_randomised_against_reference():
    rng = random.Random(1234)
    for _trial in range(200):
        size = rng.randint(1, 300)
        sm = SectorMap(size)
        ops = []
        for _ in range(rng.randint(1, 40)):
            a = rng.randint(-5, size + 5)
            b = rng.randint(-5, size + 5)
            if a > b:
                a, b = b, a
            st = rng.choice(list(State))
            ops.append((a, b, st))
            sm.set(a, b, st)
        ref = reference_states(size, ops)
        assert map_states(sm) == ref
        totals = sm.totals()
        for st in State:
            assert totals[st] == ref.count(st)
        # no two adjacent segments share a state
        segs = sm.segments()
        for left, right in zip(segs, segs[1:]):
            assert left[2] != right[2]
            assert left[1] == right[0]


def test_ranges_and_all_in():
    sm = SectorMap(1000)
    sm.set(100, 200, State.BAD)
    sm.set(200, 300, State.FAILED)
    sm.set(500, 600, State.BAD)
    assert sm.ranges([State.BAD]) == [(100, 200), (500, 600)]
    assert sm.ranges([State.BAD, State.FAILED]) == [(100, 300), (500, 600)]
    assert sm.all_in(100, 300, [State.BAD, State.FAILED])
    assert not sm.all_in(0, 300, [State.BAD, State.FAILED])


def test_ddrescue_roundtrip(tmp_path):
    sm = SectorMap(1 << 20)
    sm.set(0, 4096, State.GOOD)
    sm.set(4096, 8192, State.BAD)
    sm.set(8192, 65536, State.FAILED)
    sm.set(65536, 131072, State.SKIPPED)
    sm.current_pos = 8192
    path = tmp_path / "x.map"
    sm.save(path)
    text = path.read_text()
    assert "0x00001000  0x00001000  -" in text
    loaded = SectorMap.load(path, size=1 << 20)
    # SKIPPED is stored as non-tried
    expected = [(0, 4096, State.GOOD), (4096, 8192, State.BAD), (8192, 65536, State.FAILED),
                (65536, 1 << 20, State.UNTRIED)]
    assert loaded.segments() == expected
    assert loaded.current_pos == 8192


def test_ddrescue_parse_real_format():
    text = """# Mapfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue /dev/sdb img map
# Start time:   2024-02-01 10:00:00
# current_pos  current_status  current_pass
0x7FF0000     ?               1
#      pos        size  status
0x00000000  0x07FF0000  +
0x07FF0000  0x00010000  *
0x08000000  0x00000200  /
0x08000200  0x00000200  -
0x08000400  0x07FFFC00  ?
"""
    sm = SectorMap.from_ddrescue(text)
    assert sm.size == 0x10000000
    assert sm.state_at(0x07FF0000) == State.FAILED
    assert sm.state_at(0x08000000) == State.FAILED
    assert sm.state_at(0x08000200) == State.BAD
    assert sm.state_at(0x09000000) == State.UNTRIED


def test_bad_mapfile():
    with pytest.raises(MapFileError):
        SectorMap.from_ddrescue("0x0 ? 1\n0x0 0x10 Z\n")


def test_summarize():
    sm = SectorMap(1000)
    sm.set(0, 500, State.GOOD)
    sm.set(510, 520, State.BAD)
    sm.set(900, 950, State.GOOD)
    cells = sm.summarize(10)
    assert cells[0] == State.GOOD
    assert cells[4] == State.GOOD
    assert cells[5] == State.BAD
    assert cells[9] == PARTIAL
    assert cells[7] == State.UNTRIED


def test_align_expands_problem_areas():
    sm = SectorMap(4096)
    sm.set(0, 4096, State.GOOD)
    sm.set(700, 900, State.BAD)
    sm.align(512)
    assert sm.segments() == [(0, 512, State.GOOD), (512, 1024, State.BAD), (1024, 4096, State.GOOD)]
