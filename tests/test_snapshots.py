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


def test_keep_warm_speeds_up_as_soon_as_someone_views():
    builder = Builder()
    store = SnapshotStore(builder, max_age=timedelta(minutes=1))
    store.keep_warm(active=timedelta(minutes=5), busy_every=0.1, idle_every=600, check_every=0.02)
    time.sleep(0.1)
    assert builder.calls == 1  # 아무도 안 보면 첫 빌드 뒤로는 쉰다
    store.get()  # 누가 화면을 열었다
    time.sleep(0.3)
    assert builder.calls >= 3


def test_build_requested_during_older_build_rebuilds_with_new_data():
    # 뒤에서 도는 빌드가 DB를 읽은 뒤에 반영이 일어나고, 그 빌드가 끝나기 전에 새 빌드를 요청한 경우
    data = {"value": "before"}
    reading = threading.Event()

    def builder():
        snapshot = dict(data)
        reading.set()
        time.sleep(0.2)
        return snapshot

    store = SnapshotStore(builder, max_age=timedelta(minutes=1))
    background = threading.Thread(target=store.build)
    background.start()
    reading.wait()
    data["value"] = "after"  # 반영
    assert store.build() == {"value": "after"}  # 예전에는 'before'를 돌려줬다
    background.join()
