import threading
import time
from datetime import timedelta

from stockmate.web.snapshots import SnapshotStore


class Builder:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.delay = delay

    def __call__(self):
        self.calls += 1
        time.sleep(self.delay)
        return {"n": self.calls}


def test_first_get_builds_then_reuses():
    builder = Builder()
    store = SnapshotStore(builder, max_age=timedelta(minutes=1))
    assert store.get() == {"n": 1}
    assert store.get() == {"n": 1}
    assert builder.calls == 1


def test_stale_snapshot_is_returned_immediately_and_refreshed_in_background():
    builder = Builder(delay=0.2)
    store = SnapshotStore(builder, max_age=timedelta(0))
    store.get()  # 첫 빌드 (0.2초)
    started = time.monotonic()
    assert store.get() == {"n": 1}  # 오래된 것을 기다리지 않고 바로 받는다
    assert time.monotonic() - started < 0.1
    time.sleep(0.4)
    assert builder.calls == 2
    assert store._snapshot == {"n": 2}


def test_concurrent_builds_share_one_result():
    builder = Builder(delay=0.2)
    store = SnapshotStore(builder, max_age=timedelta(minutes=1))
    results = []
    threads = [threading.Thread(target=lambda: results.append(store.build())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 먼저 들어간 빌드가 끝난 뒤 기다리던 요청은 그 결과를 쓴다 (많아야 2번 빌드)
    assert builder.calls <= 2
    assert all(r in ({"n": 1}, {"n": 2}) for r in results)
