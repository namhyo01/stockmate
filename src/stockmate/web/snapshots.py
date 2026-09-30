"""대시보드용 스냅샷 보관소.

한투 API로 29종목 시세를 받는 데 4초쯤 걸린다. 매 요청마다 기다리지 않도록
최근 스냅샷을 바로 돌려주고, 오래됐으면 뒤에서 새로 만든다 (stale-while-revalidate).
백그라운드 스레드가 미리 갱신해 두므로 페이지를 처음 열 때도 대개 기다리지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

Snapshot = dict[str, Any]


class SnapshotStore:
    def __init__(self, builder: Callable[[], Snapshot], max_age: timedelta):
        self._builder = builder
        self._max_age = max_age
        self._build_lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._built_at: datetime | None = None
        self.last_viewed = datetime.min

    def build(self) -> Snapshot:
        """지금 새로 만든다. 다른 스레드가 만드는 중이면 그 결과를 기다려서 쓴다."""
        started = datetime.now()
        with self._build_lock:
            if self._built_at and self._built_at >= started:
                return self._snapshot  # 기다리는 동안 다른 스레드가 이미 새로 만들었다
            snapshot = self._builder()
            self._snapshot, self._built_at = snapshot, datetime.now()
            return snapshot

    def get(self) -> Snapshot:
        """최근 스냅샷을 바로 돌려준다. 오래됐으면 뒤에서 새로 만들기 시작한다."""
        self.last_viewed = datetime.now()
        if self._snapshot is None:
            return self.build()
        if datetime.now() - self._built_at > self._max_age:
            self.refresh_in_background()
        return self._snapshot

    def refresh_in_background(self) -> None:
        if not self._build_lock.locked():
            threading.Thread(target=self._build_quietly, daemon=True).start()

    def _build_quietly(self) -> None:
        try:
            self.build()
        except Exception:  # 백그라운드 갱신 실패는 다음 주기에 다시 시도한다
            log.exception("스냅샷 갱신 실패")

    def keep_warm(self, active: timedelta, busy_every: float, idle_every: float) -> None:
        """누가 보고 있으면(active 안에 요청이 있었으면) 자주, 아니면 드물게 미리 갱신한다."""

        def loop() -> None:
            self._build_quietly()
            while True:
                viewed_recently = datetime.now() - self.last_viewed < active
                time.sleep(busy_every if viewed_recently else idle_every)
                self._build_quietly()

        threading.Thread(target=loop, daemon=True, name="snapshot-warmer").start()
