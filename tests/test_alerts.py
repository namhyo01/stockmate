import datetime as dt

from stockmate import alerts

NOW = dt.datetime(2026, 11, 9, 10, 0)


def plan(id_, condition, **kw):
    return {"id": id_, "side": "buy", "name": "엔비디아", "currency": "USD", "account": "토스", "condition": condition,
            "price_now": 170.5, "price_low": None, "price_high": 172.0, **kw}  # fmt: skip


def idea(id_, **kw):
    row = {"id": id_, "name": "코스트코", "currency": "USD", "price_now": 900.0, "entry_low": 880.0, "entry_high": 910.0,
           "stop": 800.0, "target": 1000.0, "in_zone": False, "below_stop": False, "hit_target": False}  # fmt: skip
    return {**row, **kw}


def test_plan_alerts_only_met():
    out = alerts.plan_alerts([plan(1, "met"), plan(2, "wait"), plan(3, "none")])
    assert [a.key for a in out] == ["plan:1"]
    assert out[0].title == "엔비디아 매수 계획 가격이에요"
    assert out[0].body == "지금 $170.50 (계획 $172.00 이하) · 토스"


def test_idea_alerts_stop_wins_over_zone_and_won_format():
    out = alerts.idea_alerts([
        idea(1, in_zone=True),
        idea(2, in_zone=True, below_stop=True),
        idea(3, hit_target=True, currency="KRW", price_now=1_050_000.0, target=1_000_000.0),
        idea(4),
    ])  # fmt: skip
    assert [a.key for a in out] == ["idea:1:zone", "idea:2:stop", "idea:3:target"]
    assert "$880.00~$910.00" in out[0].body
    assert out[2].body == "지금 1,050,000원 (목표가 1,000,000원)"


def test_due_alerts_fires_on_entry_only():
    a = alerts.Alert("plan:1", "t", "b", "/")
    state: dict = {}
    assert alerts.due_alerts([a], state, NOW) == [a]
    assert alerts.due_alerts([a], state, NOW + dt.timedelta(minutes=1)) == []  # 계속 조건 안: 다시 안 보냄
    assert alerts.due_alerts([], state, NOW + dt.timedelta(minutes=2)) == []  # 빠져나감
    # 쿨다운 안에 다시 들어오면 보내지 않는다
    assert alerts.due_alerts([a], state, NOW + dt.timedelta(hours=1)) == []
    alerts.due_alerts([], state, NOW + dt.timedelta(hours=2))
    # 쿨다운이 지나서 다시 들어오면 보낸다
    assert alerts.due_alerts([a], state, NOW + alerts.COOLDOWN + dt.timedelta(minutes=1)) == [a]


def test_check_persists_state_and_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "DIR", tmp_path)
    sent = []
    monkeypatch.setattr(alerts, "send", lambda a, only=None: sent.append(a.key) or 1)
    assert alerts.check([plan(1, "met")], [], NOW) == []  # 처음엔 이미 든 조건을 기억만 한다
    alerts.check([plan(1, "met")], [idea(5, in_zone=True)], NOW + dt.timedelta(minutes=1))
    alerts.check([plan(1, "met")], [idea(5, in_zone=True)], NOW + dt.timedelta(minutes=2))
    assert sent == ["idea:5:zone"]
    assert (tmp_path / "state.json").exists()


def test_subscribe_replaces_same_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "DIR", tmp_path)
    sub = {"endpoint": "https://push.example/1", "keys": {"p256dh": "x", "auth": "y"}}
    alerts.subscribe(sub, "iPhone 앱")
    alerts.subscribe(sub, "iPhone 앱")
    alerts.subscribe({**sub, "endpoint": "https://push.example/2"}, "Mac")
    assert [s["endpoint"] for s in alerts.subscriptions()] == ["https://push.example/1", "https://push.example/2"]
    alerts.unsubscribe("https://push.example/1")
    assert [s["device"] for s in alerts.subscriptions()] == ["Mac"]


def test_public_key_is_raw_p256_point(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "DIR", tmp_path)
    key = alerts.public_key()
    assert len(key) == 87 and key == alerts.public_key()  # 65바이트 → base64url 87자, 한 번 만든 키를 계속 쓴다


def test_in_quiet_wraps_midnight():
    cfg = {"quiet_on": True, "quiet_start": "23:00", "quiet_end": "08:00"}
    at = lambda h, m=0: dt.datetime(2026, 11, 9, h, m)  # noqa: E731
    assert alerts.in_quiet(at(23), cfg) and alerts.in_quiet(at(3), cfg) and alerts.in_quiet(at(7, 59), cfg)
    assert not alerts.in_quiet(at(8), cfg) and not alerts.in_quiet(at(22, 59), cfg)
    day = {"quiet_on": True, "quiet_start": "12:00", "quiet_end": "13:00"}
    assert alerts.in_quiet(at(12, 30), day) and not alerts.in_quiet(at(13), day)
    assert not alerts.in_quiet(at(3), {**cfg, "quiet_on": False})
    assert not alerts.in_quiet(at(3), {**cfg, "quiet_end": "23:00"})  # 시작 = 끝이면 없음


def test_quiet_hours_hold_until_end(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "DIR", tmp_path)
    sent = []
    monkeypatch.setattr(alerts, "send", lambda a, only=None: sent.append(a.key) or 1)
    alerts.save_settings({"quiet_on": True, "quiet_start": "23:00", "quiet_end": "08:00", "other": 1})
    assert "other" not in alerts.settings_()
    night = dt.datetime(2026, 11, 9, 22, 50)
    alerts.check([], [], night)  # 첫 확인 (기억만)
    assert alerts.check([plan(1, "met")], [], night + dt.timedelta(hours=2)) == []  # 새벽 1시: 쉰다
    alerts.check([plan(1, "met")], [], night + dt.timedelta(hours=9, minutes=15))  # 8시 5분: 남아 있으니 보낸다
    assert sent == ["plan:1"]
