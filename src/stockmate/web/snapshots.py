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
        self._build_started: datetime | None = None  # 지금 스냅샷을 만들기 시작한 시각 (DB를 읽은 시점)
        self.last_viewed = datetime.min

    def build(self) -> Snapshot:
        """지금 새로 만든다. 기다리는 동안 다른 스레드가 '요청 뒤에 시작한' 빌드를 끝냈으면 그 결과를 쓴다.

        끝난 시각이 아니라 시작한 시각으로 비교한다. 요청 전에 시작해서 요청 뒤에 끝난 빌드는
        요청 직전의 DB 변경(예: 스크린샷 반영)을 못 봤을 수 있다. 예전에는 이 때문에 반영 직후 '비어 있음'이 보였다.
        """
        requested = datetime.now()
        with self._build_lock:
            if self._build_started and self._build_started >= requested:
                return self._snapshot
            started = datetime.now()
            snapshot = self._builder()
            self._snapshot, self._built_at, self._build_started = snapshot, datetime.now(), started
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

    def keep_warm(
        self, active: timedelta, busy_every: float, idle_every: float, check_every: float = 2
    ) -> None:
        """누가 보고 있으면(active 안에 요청이 있었으면) 자주, 아니면 드물게 미리 갱신한다.

        긴 주기로 통째로 자면 그사이 누가 화면을 열어도 갱신이 늦어지므로, 짧게 자주 깨서 확인한다.
        """

        def due() -> bool:
            viewed_recently = datetime.now() - self.last_viewed < active
            every = busy_every if viewed_recently else idle_every
            return self._built_at is None or datetime.now() - self._built_at >= timedelta(seconds=every)

        def loop() -> None:
            while True:
                if due():
                    self._build_quietly()
                time.sleep(check_every)

        threading.Thread(target=loop, daemon=True, name="snapshot-warmer").start()
