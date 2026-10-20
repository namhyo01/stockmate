import threading
import time

from stockmate.brokers import kis


def test_throttle_is_shared_across_clients(monkeypatch):
    """여러 KisClient가 동시에 불러도 프로그램 전체 호출 간격을 지킨다."""
    monkeypatch.setattr(kis, "MIN_CALL_INTERVAL", 0.05)
    monkeypatch.setattr(kis, "_last_call", 0.0)
    stamps = []

    def call():
        kis._wait_turn()
        stamps.append(time.monotonic())

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.045  # 스레드(클라이언트)가 달라도 겹치지 않는다


def test_ledger_calls_are_spaced_wider(monkeypatch):
    monkeypatch.setattr(kis, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(kis, "LEDGER_CALL_INTERVAL", 0.1)
    monkeypatch.setattr(kis, "_last_call", 0.0)
    monkeypatch.setattr(kis, "_last_ledger_call", 0.0)
    t0 = time.monotonic()
    kis._wait_turn("TTTC8434R")
    kis._wait_turn("FHKST01010100")  # 시세 조회는 원장 간격을 기다리지 않는다
    assert time.monotonic() - t0 < 0.05
    kis._wait_turn("TTTC8434R")
    assert time.monotonic() - t0 >= 0.09
