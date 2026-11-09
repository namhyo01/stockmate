"""가격 알림 (웹 푸시): 계획 탭의 가격 조건, 추천 탭의 매수 구간·철회 기준·목표가에 시세가 닿으면 폰으로 알린다.

- 웹 서버 스레드가 1분마다 확인한다 (알림을 켠 기기가 있을 때만).
- 조건에 '새로 들어올 때'만 보낸다. 들어가 있는 동안은 다시 보내지 않고, 빠져나갔다 다시 들어와도
  같은 알림은 COOLDOWN 안에는 보내지 않는다 (구간 경계에서 오르내릴 때 알림이 쏟아지지 않게).
- 조용한 시간(설정)에는 급한 것(철회 기준 깨짐, 매도 계획 가격)만 바로 보내고, 나머지는 모아 뒀다가
  조용한 시간이 끝나면 '밤사이 n건' 알림 하나로 묶어 보낸다 (아침 요약).
- 푸시 서버 키(VAPID), 알림을 켠 기기 목록, 설정, 보낸 기록은 data/alerts/에 둔다.
- 아이폰은 사파리에서 '홈 화면에 추가'한 대시보드에서만 웹 푸시를 받을 수 있다 (iOS 16.4 이상).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stockmate.config import settings

log = logging.getLogger(__name__)

DIR = settings.db_path.parent / "alerts"
COOLDOWN = dt.timedelta(hours=12)
# 푸시 서버가 문제 있을 때 연락할 주소 (py_vapid는 mailto만 받는다). 공개 저장소라 GitHub noreply 주소를 쓴다
VAPID_SUB = "mailto:34156840+namhyo01@users.noreply.github.com"

_lock = threading.Lock()


@dataclass(frozen=True)
class Alert:
    key: str  # 같은 조건을 알아보는 이름 (plan:12, idea:3:zone)
    title: str
    body: str
    url: str  # 알림을 누르면 열 화면
    urgent: bool = False  # 조용한 시간에도 바로 보낸다 (철회 기준 깨짐, 매도 계획 가격)


def _price(value: float | None, currency: str) -> str:
    if value is None:
        return "-"
    if currency == "USD":
        return f"${value:,.2f}"
    return f"{value:,.0f}원"


def _range(low: float | None, high: float | None, currency: str) -> str:
    if low and high:
        return f"{_price(low, currency)}~{_price(high, currency)}"
    if low:
        return f"{_price(low, currency)} 이상"
    return f"{_price(high, currency)} 이하"


def plan_alerts(rows: list[dict[str, Any]]) -> list[Alert]:
    """plans.plan_rows()의 열린 계획 중 가격 조건에 들어온 것."""
    out = []
    for r in rows:
        if r["condition"] != "met":
            continue
        side = "매수" if r["side"] == "buy" else "매도"
        cur = r["currency"]
        where = f" · {r['account']}" if r.get("account") else ""
        out.append(Alert(
            key=f"plan:{r['id']}",
            title=f"{r['name']} {side} 계획 가격이에요",
            body=f"지금 {_price(r['price_now'], cur)} (계획 {_range(r['price_low'], r['price_high'], cur)}){where}",
            url="/#plans",
            urgent=r["side"] == "sell",
        ))  # fmt: skip
    return out


def idea_alerts(rows: list[dict[str, Any]]) -> list[Alert]:
    """ideas.idea_rows()의 추천 카드 중 매수 구간에 들어왔거나 철회 기준·목표가에 닿은 것."""
    out = []
    for r in rows:
        cur, now = r["currency"], _price(r["price_now"], r["currency"])
        if r["below_stop"]:
            out.append(Alert(f"idea:{r['id']}:stop", f"{r['name']} 철회 기준을 깼어요",
                             f"지금 {now} (철회 기준 {_price(r['stop'], cur)}). 종가로도 깨지면 추천을 거둬요", "/#ideas", urgent=True))  # fmt: skip
        elif r["hit_target"]:
            out.append(Alert(f"idea:{r['id']}:target", f"{r['name']} 목표가에 닿았어요",
                             f"지금 {now} (목표가 {_price(r['target'], cur)})", "/#ideas"))  # fmt: skip
        elif r["in_zone"]:
            out.append(Alert(f"idea:{r['id']}:zone", f"{r['name']} AI 매수 구간이에요",
                             f"지금 {now} (사는 가격대 {_range(r['entry_low'], r['entry_high'], cur)})", "/#ideas"))  # fmt: skip
    return out


DEFAULT_SETTINGS = {"quiet_on": False, "quiet_start": "23:00", "quiet_end": "08:00"}


def settings_() -> dict[str, Any]:
    return {**DEFAULT_SETTINGS, **_read("settings.json", {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        merged = {**settings_(), **{k: v for k, v in values.items() if k in DEFAULT_SETTINGS}}
        _write("settings.json", merged)
    return merged


def in_quiet(now: dt.datetime, cfg: dict[str, Any]) -> bool:
    """조용한 시간인지. 23:00~08:00처럼 자정을 넘는 구간도 된다. 시작과 끝이 같으면 조용한 시간이 없다."""
    if not cfg.get("quiet_on"):
        return False
    start, end, t = cfg["quiet_start"], cfg["quiet_end"], now.strftime("%H:%M")
    if start == end:
        return False
    return start <= t < end if start < end else t >= start or t < end


def due_alerts(current: list[Alert], state: dict[str, Any], now: dt.datetime) -> list[Alert]:
    """지금 조건에 든 알림 중 보낼 것. state(active, fired)를 고친다."""
    active = set(state.get("active", []))
    fired = state.setdefault("fired", {})
    send = []
    for a in current:
        last = fired.get(a.key)
        if a.key in active or (last and now - dt.datetime.fromisoformat(last) < COOLDOWN):
            continue
        send.append(a)
        fired[a.key] = now.isoformat(timespec="seconds")
    state["active"] = sorted(a.key for a in current)
    cutoff = now - COOLDOWN * 4  # 오래된 기록은 정리한다
    state["fired"] = {k: v for k, v in fired.items() if dt.datetime.fromisoformat(v) > cutoff}
    return send


# ── 저장 ─────────────────────────────────────────────


def _read(name: str, default: Any) -> Any:
    path = DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write(name: str, value: Any) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{name}.tmp"
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DIR / name)


def _vapid_path() -> Path:
    """푸시 서버 키. 처음 쓸 때 만든다 (바꾸면 모든 기기에서 알림을 다시 켜야 한다)."""
    from py_vapid import Vapid01

    path = DIR / "vapid.pem"
    with _lock:
        if not path.exists():
            DIR.mkdir(parents=True, exist_ok=True)
            key = Vapid01()
            key.generate_keys()
            key.save_key(str(path))
    return path


def public_key() -> str:
    """브라우저 구독에 넘기는 공개 키 (base64url, 압축하지 않은 점)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid01

    raw = Vapid01.from_file(str(_vapid_path())).public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscriptions() -> list[dict[str, Any]]:
    return _read("subscriptions.json", [])


def subscribe(sub: dict[str, Any], device: str) -> None:
    with _lock:
        subs = [s for s in subscriptions() if s["endpoint"] != sub["endpoint"]]
        subs.append({**sub, "device": device[:200], "added_at": dt.datetime.now().isoformat(timespec="seconds")})
        _write("subscriptions.json", subs)


def unsubscribe(endpoint: str) -> None:
    with _lock:
        _write("subscriptions.json", [s for s in subscriptions() if s["endpoint"] != endpoint])


def send(alert: Alert, only: str | None = None) -> int:
    """알림을 켠 기기 모두(only면 그 기기만)에 보낸다. 받은 기기 수. 없어진 구독(404·410)은 지운다."""
    from pywebpush import WebPushException, webpush

    key = str(_vapid_path())
    payload = json.dumps({"title": alert.title, "body": alert.body, "url": alert.url, "tag": alert.key}, ensure_ascii=False)
    ok, gone = 0, []
    for sub in subscriptions():
        if only and sub["endpoint"] != only:
            continue
        info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
        try:
            webpush(info, payload, vapid_private_key=key, vapid_claims={"sub": VAPID_SUB}, ttl=3600, timeout=10)
            ok += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                gone.append(sub["endpoint"])
            else:
                log.warning("웹 푸시 실패 (%s): %s", status, e)
        except Exception as e:  # 네트워크 오류 등은 다음 알림 때 다시 시도한다
            log.warning("웹 푸시 실패: %s", e)
    for endpoint in gone:
        unsubscribe(endpoint)
    return ok


def check(plan_rows: list[dict[str, Any]], idea_rows: list[dict[str, Any]], now: dt.datetime | None = None) -> list[Alert]:
    """지금 시세로 조건을 보고 새로 닿은 것을 보낸다. 보낸 알림 목록.

    처음 확인할 때는 이미 조건에 든 것(오래전에 철회 기준을 깬 추천 등)을 기억만 하고 보내지 않는다.
    알림을 켜자마자 수십 개가 쏟아지지 않게.
    """
    now = now or dt.datetime.now()
    quiet = in_quiet(now, settings_())
    current = plan_alerts(plan_rows) + idea_alerts(idea_rows)
    with _lock:
        state = _read("state.json", {})
        first = "active" not in state
        due = due_alerts(current, state, now)
        if first:
            due = []
        held = state.setdefault("held", [])
        if quiet:  # 급하지 않은 것은 모아 둔다
            held += [{"title": a.title, "url": a.url} for a in due if not a.urgent]
            due = [a for a in due if a.urgent]
        elif held:
            due.insert(0, digest(held))
            state["held"] = []
        _write("state.json", state)
    for alert in due:
        send(alert)
    return due


def digest(held: list[dict[str, str]]) -> Alert:
    """조용한 시간 동안 모아 둔 알림을 하나로 (아침 요약)."""
    titles = [h["title"] for h in held]
    body = " · ".join(titles[:4]) + (f" 외 {len(titles) - 4}건" if len(titles) > 4 else "")
    url = "/#plans" if any(h["url"] == "/#plans" for h in held) else "/#ideas"
    return Alert("digest", f"조용한 시간 동안 {len(titles)}건 있었어요", body, url)


class AlertWatcher:
    """웹 서버에서 1분마다 check()를 부른다. rows()는 (열린 계획, 추천 카드) 행을 돌려준다."""

    def __init__(self, rows):
        self._rows = rows

    def start(self, every: int = 60) -> None:
        def loop() -> None:
            import time

            while True:
                try:
                    if subscriptions():
                        plan_rows, idea_rows = self._rows()
                        for a in check(plan_rows, idea_rows):
                            log.info("알림 보냄: %s", a.title)
                except Exception:
                    log.exception("가격 알림 확인 실패")
                time.sleep(every)

        threading.Thread(target=loop, daemon=True, name="alert-watcher").start()
