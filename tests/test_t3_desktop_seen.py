import ctypes as C
import json

import pytest
from test_t3 import connector, thread

from herdeck.model import Status
from herdeck.t3_desktop_seen import _UI_KEY, DesktopSeen, LevelDb, visits_from_value

ENV = 'environment-1'
TID = 'thread-1'
COMPLETED = '2026-09-07T01:20:20.903Z'


def encoded(visits, tag=1):
    return bytes([tag]) + json.dumps({'threadLastVisitedAtById': visits}).encode('latin-1' if tag else 'utf-16-le')


@pytest.fixture
def db_writer(tmp_path):
    try:
        lib = LevelDb()
    except Exception:
        pytest.skip('Native LevelDB required for snapshot integration tests')
    ptr, size, string = C.c_void_p, C.c_size_t, C.c_char_p
    for name, result, args in [
        ('options_set_create_if_missing', None, [ptr, C.c_ubyte]),
        ('writeoptions_create', ptr, []), ('writeoptions_destroy', None, [ptr]),
        ('put', None, [ptr, ptr, string, size, string, size, C.POINTER(ptr)]),
        ('delete', None, [ptr, ptr, string, size, C.POINTER(ptr)]),
        ('compact_range', None, [ptr, string, size, string, size]),
    ]:
        f = getattr(lib.lib, 'leveldb_' + name)
        f.restype, f.argtypes = result, args
        setattr(lib, name, f)
    path = tmp_path / 'live'
    options = lib.options_create()
    lib.options_set_create_if_missing(options, 1)
    err = ptr()
    database = lib.open(options, str(path).encode(), C.byref(err))
    lib.check(err)
    write_options = lib.writeoptions_create()

    def write(value, key=_UI_KEY, compact=False):
        error = ptr()
        if value is None:
            lib.delete(database, write_options, key, len(key), C.byref(error))
        else:
            lib.put(database, write_options, key, len(key), value, len(value), C.byref(error))
        lib.check(error)
        if compact:
            lib.compact_range(database, None, 0, None, 0)
    yield path, write
    lib.close(database)
    lib.writeoptions_destroy(write_options)
    lib.options_destroy(options)


@pytest.mark.parametrize('tag', [0, 1])
def test_chromium_string_decoding(tag):
    assert visits_from_value(encoded({ENV + ':' + TID: COMPLETED}, tag)) == {ENV + ':' + TID: COMPLETED}
    assert visits_from_value(None) == {}


def test_live_locked_database_is_read_via_copy_and_updates_survive_compaction(db_writer):
    path, write = db_writer
    write(encoded({ENV + ':' + TID: COMPLETED}))
    before = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    seen = DesktopSeen(path)
    seen.refresh()
    assert seen.last_error is None
    assert seen.get(ENV, TID) == COMPLETED
    assert seen.get('another-environment', TID) is None
    assert before == {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    # Mark unread can move the local marker backward; never pick max historical timestamp.
    unread = '2026-09-07T01:20:20.902Z'
    write(encoded({ENV + ':' + TID: unread}), compact=True)
    seen.refresh()
    assert seen.get(ENV, TID) == unread
    write(None, compact=True)
    seen.refresh()
    assert seen.get(ENV, TID) is None


def test_only_expected_electron_origin_is_read(db_writer):
    path, write = db_writer
    write(encoded({ENV + ':' + TID: COMPLETED}), key=b'_https://unrelated\x00\x01t3code:ui-state:v1')
    seen = DesktopSeen(path)
    seen.refresh()
    assert seen.last_error and seen.get(ENV, TID) is None


def test_failed_snapshot_drops_stale_visits_and_recovers(db_writer, monkeypatch):
    from herdeck import t3_desktop_seen as module
    path, write = db_writer
    write(encoded({ENV + ':' + TID: COMPLETED}))
    seen = DesktopSeen(path)
    seen.refresh()
    assert seen.get(ENV, TID) == COMPLETED
    real_inventory = module.inventory
    monkeypatch.setattr(module, 'inventory', lambda _: (_ for _ in ()).throw(OSError('test')))
    seen.refresh()
    assert seen.last_error and seen.get(ENV, TID) is None
    monkeypatch.setattr(module, 'inventory', real_inventory)
    seen.refresh()
    assert seen.last_error is None and seen.get(ENV, TID) == COMPLETED


def test_copy_race_is_not_accepted(db_writer, monkeypatch):
    from herdeck import t3_desktop_seen as module
    path, write = db_writer
    write(encoded({ENV + ':' + TID: COMPLETED}))
    real_copy = module.shutil.copyfile
    def racing_copy(src, dest):
        result = real_copy(src, dest)
        write(encoded({ENV + ':' + TID: '2026-09-07T02:00:00Z'}))
        return result
    monkeypatch.setattr(module.shutil, 'copyfile', racing_copy)
    seen = DesktopSeen(path)
    seen.refresh()
    assert seen.last_error and seen.get(ENV, TID) is None


@pytest.mark.asyncio
async def test_desktop_visit_clears_done_and_new_completion_returns_without_manual_seen(db_writer, tmp_path, monkeypatch):
    from herdeck.t3_seen import SeenStore
    path, write = db_writer
    monkeypatch.setenv('HERDECK_T3_DESKTOP_READ_STATE', '1')
    monkeypatch.setenv('HERDECK_T3_DESKTOP_STORAGE', str(path))
    c, _ = connector(thread(latestTurn={'state': 'completed', 'completedAt': COMPLETED}))
    c._seen = SeenStore(tmp_path / 'manual', 't3')
    get = c.http.get
    c.http.get = lambda route: {'serverVersion': '0.0.38', 'environmentId': ENV} if route.endswith('/environment') else get(route)
    await c.refresh()
    assert c.states[TID].status == Status.DONE
    write(encoded({ENV + ':' + TID: '2026-09-07T01:20:20.903+00:00'}))
    await c.refresh()
    assert c.states[TID].status == Status.IDLE
    assert not c._seen.path.exists()
    c.http.thread['latestTurn']['completedAt'] = '2026-09-07T02:00:00Z'
    await c.refresh()
    assert c.states[TID].status == Status.DONE
    assert 'acknowledge' not in c.states[TID].capabilities
    assert not c.http.writes


@pytest.mark.asyncio
async def test_valid_empty_visit_map_matches_desktop_never_visited_semantics(db_writer, monkeypatch):
    path, write = db_writer
    write(encoded({}))
    monkeypatch.setenv('HERDECK_T3_DESKTOP_READ_STATE', '1')
    monkeypatch.setenv('HERDECK_T3_DESKTOP_STORAGE', str(path))
    c, _ = connector(thread(latestTurn={'state': 'completed', 'completedAt': COMPLETED}))
    get = c.http.get
    c.http.get = lambda route: {'serverVersion': '0.0.38', 'environmentId': ENV} if route.endswith('/environment') else get(route)
    await c.refresh()
    assert c.states[TID].status == Status.IDLE
    write(encoded({ENV + ':' + TID: '2026-09-07T01:20:20.902Z'}))
    await c.refresh()
    assert c.states[TID].status == Status.DONE
