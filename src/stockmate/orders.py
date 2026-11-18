"""토스 지정가 주문: 계획 탭의 '토스로 주문' 버튼.

AI에게는 매매 경로를 주지 않는다. 이 모듈은 웹 화면에서 사람이 Google OTP 코드를 넣었을 때만 불린다
(AI 응답, 가격 알림, 예약 작업은 부르지 않는다). 토스에는 모의투자 서버가 없어서 첫 주문부터 실제 돈이다.

지키는 것
- 토스 계좌의 열린 매매 계획만, 지정가(당일 유효)만, 정수 수량만. 매수·매도 방향은 계획 그대로
- 한 번에 PER_ORDER_KRW, 최근 24시간 DAILY_KRW까지 (매수·매도 합쳐서 주문 금액으로 센다. 취소·거부된 주문은 체결된 만큼만.
  미국 주식은 환전 스프레드를 생각해 환율에 FX_BUFFER를 얹어 센다. 24시간으로 세는 건 미국 장이 자정을 넘기 때문)
- 같은 종목·방향에 끝나지 않은 주문(응답을 못 받은 것 포함)이 있으면 새 주문을 받지 않는다
- 토스가 확실히 거절한 것(400·401·422·429, 409 중 처리 중이 아닌 것)만 '거부됨'으로 남긴다. 나머지 오류는
  주문이 들어갔을 수도 있어서 '확인 필요'로 남기고 한도에도 그대로 센다. 사람이 토스 앱에서 보고 정리한다(resolve)
- 지금 가격보다 MAX_GAP 넘게 비싸게 사거나 싸게 파는 주문은 막는다 (바로 체결돼서 손해 보는 실수)
- 주문마다 Google OTP(TOTP, 30초마다 바뀌는 6자리)를 넣어야 한다. 한 코드는 한 번만 쓴다.
  10분에 OTP_TRIES번 틀리면 잠시, 하루에 OTP_HARD_TRIES번 틀리면 터미널에서 풀 때까지(`stock order-otp --unlock`) 막는다.
  틀릴 때마다 폰에 알림을 보낸다. 등록은 서버 터미널에서만 (`stock order-otp`):
  웹 화면에는 비밀 키를 보여 주지 않아서, 대시보드에 들어온 사람이 새 폰을 등록할 수 없다
- 주문 기록은 data/orders/orders.json에 남긴다. 전부 체결되면 계획을 '했어요'로 닫는다
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.orm import Session

from stockmate import plans
from stockmate.brokers.toss import BUSY_CODES, TossClient, TossError
from stockmate.config import settings
from stockmate.market_data import PriceProvider
from stockmate.models import Market, PlanItem
from stockmate.portfolio import detect_market

DIR = settings.db_path.parent / "orders"
ACCOUNT = "토스"
PER_ORDER_KRW = Decimal(1_000_000)
DAILY_KRW = PER_ORDER_KRW * 3
DAY = dt.timedelta(hours=24)
FX_BUFFER = Decimal("1.01")
MAX_GAP = Decimal("0.05")
OTP_TRIES = 5
OTP_LOCK = dt.timedelta(minutes=10)
OTP_HARD_TRIES = 20
OTP_STEP = 30  # 초
OTP_ISSUER = "stockmate"
# 토스 주문 상태 중 아직 끝나지 않은 것 (UNKNOWN: 보냈는데 응답을 못 받음)
OPEN_STATUSES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE", "UNKNOWN"}
STATUS_LABELS = {
    "PENDING": "체결 대기", "PARTIAL_FILLED": "일부 체결", "PENDING_CANCEL": "취소 중", "PENDING_REPLACE": "정정 중",
    "FILLED": "체결", "CANCELED": "취소됨", "REJECTED": "거부됨", "CANCEL_REJECTED": "취소 거부",
    "REPLACE_REJECTED": "정정 거부", "REPLACED": "정정됨", "UNKNOWN": "확인 필요", "MANUAL": "앱에서 확인",
}  # fmt: skip
# 토스가 이 주문을 확실히 처리하지 않았다는 답 (그 밖의 오류는 들어갔을 수도 있다)
_REFUSED = {400, 401, 403, 404, 422, 429}

_lock = threading.Lock()


def _notify(title: str, body: str) -> None:
    """OTP가 틀렸을 때 폰으로 알린다 (알림을 켠 기기에). 테스트에서는 바꿔 끼운다."""
    from stockmate import alerts

    alert = alerts.Alert("order-otp", title, body, "/#plans", urgent=True)
    threading.Thread(target=alerts.send, args=(alert,), daemon=True).start()


class OrderError(Exception):
    pass


def enabled() -> bool:
    """토스 키와 Google OTP 등록이 모두 있어야 주문 버튼이 켜진다."""
    return bool(settings.toss_client_id and settings.toss_client_secret and _otp().get("secret"))


# ── Google OTP (TOTP, RFC 6238) ──────────────────────


def _otp() -> dict[str, Any]:
    try:
        return json.loads((DIR / "otp.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_otp(value: dict[str, Any]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / "otp.json"
    tmp = DIR / "otp.json.tmp"
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def setup_otp(reset: bool = False) -> str:
    """비밀 키를 만들어 저장하고 Google OTP 앱에 넣을 otpauth 주소를 돌려준다. 이미 있으면 reset일 때만 바꾼다."""
    with _lock:
        if _otp().get("secret") and not reset:
            raise OrderError("이미 등록돼 있어요. 새 폰으로 바꾸려면 --reset (예전 폰의 코드는 더는 안 돼요).")
        secret = base64.b32encode(secrets.token_bytes(20)).decode()
        _save_otp({"secret": secret, "last_step": 0, "fails": [], "created_at": dt.datetime.now().isoformat(timespec="seconds")})
    return otp_uri(secret)


def unlock_otp() -> None:
    """많이 틀려서 잠긴 것을 푼다 (터미널에서만)."""
    with _lock:
        saved = _otp()
        if saved:
            _save_otp({**saved, "fails": [], "locked": False})


def otp_uri(secret: str) -> str:
    return f"otpauth://totp/{OTP_ISSUER}:toss-order?secret={secret}&issuer={OTP_ISSUER}&digits=6&period={OTP_STEP}"


def totp(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_otp(code: str, now: dt.datetime | None = None) -> None:
    """맞으면 그 코드를 쓴 것으로 남긴다 (같은 코드로 두 번 주문할 수 없다). 시계 차이로 앞뒤 30초까지 받는다.

    틀린 기록은 파일에 남겨서 서버를 다시 켜도 이어진다.
    """
    now = now or dt.datetime.now()
    code = code.strip().replace(" ", "")
    with _lock:
        saved = _otp()
        if not saved.get("secret"):
            raise OrderError("Google OTP가 등록돼 있지 않아요. 서버에서 `stock order-otp`로 등록해 주세요.")
        if saved.get("locked"):
            raise OrderError("OTP를 너무 많이 틀려서 주문을 막았어요. 서버 터미널에서 `stock order-otp --unlock`으로 풀어 주세요.")
        fails = [t for t in saved.get("fails", []) if now - dt.datetime.fromisoformat(t) < DAY]
        recent = [t for t in fails if now - dt.datetime.fromisoformat(t) < OTP_LOCK]
        if len(recent) >= OTP_TRIES:
            raise OrderError(f"OTP를 {OTP_TRIES}번 틀려서 {OTP_LOCK.seconds // 60}분 동안 주문을 막았어요.")
        current = int(now.timestamp()) // OTP_STEP
        matched = next(
            (step for step in (current - 1, current, current + 1)
             if hmac.compare_digest(totp(saved["secret"], step), code)),
            None,
        )  # fmt: skip
        if matched is not None and matched <= saved.get("last_step", 0):
            raise OrderError("이미 쓴 OTP 코드예요. 앱에서 새 코드가 나오면 넣어 주세요.")
        if matched is None:
            fails.append(now.isoformat(timespec="seconds"))
            locked = len(fails) >= OTP_HARD_TRIES
            _save_otp({**saved, "fails": fails, "locked": locked})
            left = OTP_TRIES - len(recent) - 1
        else:
            _save_otp({**saved, "last_step": matched, "fails": fails})
    if matched is None:
        _notify("토스 주문 OTP가 틀렸어요", "본인이 아니면 대시보드 접근을 확인하세요" + (" · 주문을 막았어요" if locked else ""))
        raise OrderError(f"OTP 코드가 맞지 않아요 ({max(left, 0)}번 남았어요).")


# ── 기록 ─────────────────────────────────────────────


def records() -> list[dict[str, Any]]:
    try:
        return json.loads((DIR / "orders.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _save(rows: list[dict[str, Any]]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "orders.json.tmp"
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DIR / "orders.json")


def counted_krw(rec: dict[str, Any]) -> Decimal:
    """한도에 세는 금액. 취소·거부된 주문은 체결된 만큼만."""
    amount = Decimal(rec["amount_krw"])
    if rec["status"] in ("CANCELED", "REJECTED", "REPLACED"):
        return amount * Decimal(rec.get("filled_quantity") or 0) / Decimal(rec["quantity"])
    return amount


def used_today(rows: list[dict[str, Any]], now: dt.datetime) -> Decimal:
    """최근 24시간에 쓴 한도."""
    return sum(
        (counted_krw(r) for r in rows if now - dt.datetime.fromisoformat(r["ordered_at"]) < DAY), Decimal(0)
    )


# ── 확인 ─────────────────────────────────────────────


def _tick_price(price: Decimal, market: Market) -> Decimal:
    if market is Market.KRX:
        if price != price.to_integral_value():
            raise OrderError("국내 주식 가격은 원 단위 정수로 넣어 주세요.")
        return price.quantize(Decimal(1))
    step = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    if price != price.quantize(step, rounding=ROUND_DOWN):
        raise OrderError(f"미국 주식 가격은 {'소수점 둘째' if step == Decimal('0.01') else '소수점 넷째'} 자리까지 넣어 주세요.")
    return price.quantize(step)


def validate(
    item: PlanItem, quantity: int, price: Decimal, price_now: Decimal | None, fx: Decimal, used: Decimal
) -> tuple[Market, Decimal, Decimal]:
    """주문해도 되는지 본다. (시장, 맞춘 가격, 원화 주문 금액). 안 되면 OrderError."""
    if item.status != plans.OPEN:
        raise OrderError("열린 계획만 주문할 수 있어요.")
    if item.account != ACCOUNT:
        raise OrderError("토스 계좌 계획만 주문할 수 있어요.")
    if not item.symbol:
        raise OrderError("종목 코드가 없는 계획이에요. AI 탭에서 코드를 정해 주세요.")
    if not isinstance(quantity, int) or quantity <= 0:
        raise OrderError("수량은 1주 이상 정수로 넣어 주세요.")
    if price <= 0:
        raise OrderError("가격을 넣어 주세요.")
    market = item.market or detect_market(item.symbol)
    price = _tick_price(price, market)
    if price_now is None:
        raise OrderError("지금 가격을 몰라서 주문 가격이 맞는지 확인할 수 없어요. 잠시 후 다시 해 주세요.")
    buy = item.side.value == "buy"
    if buy and price > price_now * (1 + MAX_GAP):
        raise OrderError(f"지금 가격보다 {MAX_GAP:.0%} 넘게 비싸게 사는 주문이에요. 가격을 확인해 주세요.")
    if not buy and price < price_now * (1 - MAX_GAP):
        raise OrderError(f"지금 가격보다 {MAX_GAP:.0%} 넘게 싸게 파는 주문이에요. 가격을 확인해 주세요.")
    amount = (Decimal(quantity) * price * (fx * FX_BUFFER if market is Market.US else 1)).quantize(Decimal(1))
    if amount > PER_ORDER_KRW:
        raise OrderError(f"한 번에 {PER_ORDER_KRW:,.0f}원까지만 주문할 수 있어요 (이 주문 약 {amount:,.0f}원).")
    if used + amount > DAILY_KRW:
        raise OrderError(f"24시간 주문 한도 {DAILY_KRW:,.0f}원을 넘어요 (최근 24시간 {used:,.0f}원).")
    return market, price, amount


def _toss_symbol(symbol: str, market: Market) -> str:
    # 시세 조회용 BRK-B 표기를 토스 표기 BRK.B로 되돌린다
    return symbol if market is Market.KRX else symbol.replace("-", ".")


# ── 주문 ─────────────────────────────────────────────


def _blocking(rows: list[dict[str, Any]], item: PlanItem) -> dict[str, Any] | None:
    """같은 계획이나 같은 종목·방향에 아직 끝나지 않은 주문."""
    return next(
        (r for r in rows if r["status"] in OPEN_STATUSES
         and (r["plan_id"] == item.id or (r["symbol"] == item.symbol and r["side"] == item.side.value))),
        None,
    )  # fmt: skip


def place(
    session: Session,
    plan_id: int,
    quantity: int,
    price: Decimal | str | float,
    otp: str,
    provider: PriceProvider,
    client: TossClient,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """계획 하나를 토스에 지정가로 주문한다. 남긴 주문 기록."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    try:
        price = Decimal(str(price))
    except InvalidOperation:
        raise OrderError("가격을 숫자로 넣어 주세요.") from None
    item = session.get(PlanItem, plan_id)
    if item is None:
        raise OrderError("계획을 찾을 수 없어요.")
    market = item.market or detect_market(item.symbol or "")
    price_now = provider.last_prices([(market, item.symbol)]).get((market, item.symbol)) if item.symbol else None
    fx = provider.fx_to_krw("USD") if market is Market.US else Decimal(1)

    def check(rows: list[dict[str, Any]]) -> tuple[Market, Decimal, Decimal]:
        if (r := _blocking(rows, item)) is not None:
            raise OrderError(f"{r['name']}에 아직 끝나지 않은 주문이 있어요 ({STATUS_LABELS[r['status']]}). 체결되거나 취소한 뒤에 주문해 주세요.")
        return validate(item, quantity, price, price_now, fx, used_today(rows, now))

    check(records())  # 안 되는 주문이면 OTP 코드를 쓰기 전에 알린다
    client.account_seq()  # 계좌 확인 실패는 주문을 보내기 전이라 안전하다
    check_otp(otp, now)
    with _lock:  # 한도·겹침 확인과 기록을 한 번에 (두 번 눌러도 하나만)
        rows = records()
        market, price, amount = check(rows)
        rec = {
            "client_order_id": f"sm-{plan_id}-{uuid.uuid4().hex[:16]}",
            "order_id": None,
            "plan_id": plan_id,
            "name": item.name,
            "symbol": item.symbol,
            "market": market.value,
            "side": item.side.value,
            "quantity": quantity,
            "price": f"{price:f}",
            "currency": market.currency,
            "fx": f"{fx:f}",
            "amount_krw": f"{amount:f}",
            "ordered_at": now.isoformat(),
            "status": "UNKNOWN",
            "filled_quantity": "0",
            "avg_price": None,
        }
        rows.append(rec)
        _save(rows)  # 보내기 전에 남긴다: 응답을 못 받아도 한도에 들어가고, 같은 종목 주문을 막는다

    side = "BUY" if item.side.value == "buy" else "SELL"
    try:
        order_id = client.place_limit_order(rec["client_order_id"], _toss_symbol(item.symbol, market), side, quantity, price)
    except TossError as e:
        if e.status in _REFUSED or (e.status == 409 and e.code not in BUSY_CODES):
            _update(rec["client_order_id"], status="REJECTED")
            raise
        _update(rec["client_order_id"], error=str(e))
        raise OrderError(f"토스 답이 분명하지 않아요 ({e}). 주문이 들어갔을 수도 있으니 토스 앱에서 꼭 확인해 주세요.") from None
    except Exception as e:  # noqa: BLE001 연결 끊김, 응답 해석 실패: 들어갔을 수도 있다
        _update(rec["client_order_id"], error=f"{type(e).__name__}: {e}")
        raise OrderError("토스에서 답을 받지 못했어요. 주문이 들어갔을 수도 있으니 토스 앱에서 꼭 확인해 주세요.") from None
    return _update(rec["client_order_id"], order_id=order_id, status="PENDING")


def _update(client_order_id: str, expect: str | None = None, **changes: Any) -> dict[str, Any]:
    """기록 하나를 고친다. expect면 상태가 그대로일 때만 (그 사이에 취소를 눌렀으면 덮어쓰지 않는다)."""
    with _lock:
        rows = records()
        rec = next(r for r in rows if r["client_order_id"] == client_order_id)
        if expect is None or rec["status"] == expect:
            rec.update(changes)
            _save(rows)
        return rec


def refresh(session: Session, client: TossClient, now: dt.datetime | None = None) -> list[str]:
    """끝나지 않은 최근 주문의 상태를 토스에서 다시 받는다. 전부 체결되면 계획을 '했어요'로 닫는다. 실패한 것들의 이유."""
    now = now or dt.datetime.now()
    errors = []
    for rec in records():
        recent = now - dt.datetime.fromisoformat(rec["ordered_at"]) < dt.timedelta(days=7)
        if not (rec["order_id"] and rec["status"] in OPEN_STATUSES and recent):
            continue
        try:
            order = client.order(rec["order_id"])
        except (TossError, httpx.HTTPError) as e:  # 한 건이 안 돼도 나머지는 받는다
            errors.append(f"{rec['name']}: {e}")
            continue
        execution = order.get("execution") or {}
        rec = _update(
            rec["client_order_id"], expect=rec["status"], status=order.get("status") or rec["status"],
            filled_quantity=execution.get("filledQuantity") or "0", avg_price=execution.get("averageFilledPrice"),
        )  # fmt: skip
        item = session.get(PlanItem, rec["plan_id"])
        if rec["status"] == "FILLED" and item is not None and item.status == plans.OPEN:
            plans.set_status(session, item.id, plans.DONE)
    return errors


def cancel(client_order_id: str, client: TossClient) -> dict[str, Any]:
    rec = next((r for r in records() if r["client_order_id"] == client_order_id), None)
    if rec is None or not rec["order_id"]:
        raise OrderError("주문 번호를 몰라서 취소할 수 없어요. 토스 앱에서 취소해 주세요.")
    if rec["status"] not in OPEN_STATUSES:
        raise OrderError("이미 끝난 주문이에요.")
    client.cancel_order(rec["order_id"])
    return _update(client_order_id, status="PENDING_CANCEL")


def resolve(client_order_id: str, placed: bool) -> dict[str, Any]:
    """응답을 못 받은 주문을 사람이 토스 앱에서 보고 정리한다.

    placed=False(안 들어감)면 거부됨으로 한도에서 뺀다. True면 '앱에서 확인'으로 닫는다 (한도에는 그대로 센다,
    체결·취소는 토스 앱에서 하고 계획은 손으로 닫는다).
    """
    rec = next((r for r in records() if r["client_order_id"] == client_order_id), None)
    if rec is None or rec["status"] != "UNKNOWN":
        raise OrderError("확인이 필요한 주문이 아니에요.")
    return _update(client_order_id, expect="UNKNOWN", status="MANUAL" if placed else "REJECTED")


def state(rows: list[dict[str, Any]], now: dt.datetime | None = None, limit: int = 20) -> dict[str, Any]:
    """화면용: 켜졌는지, 한도, 최근 주문."""
    now = now or dt.datetime.now()
    shown = [{**r, "status_label": STATUS_LABELS.get(r["status"], r["status"]), "open": r["status"] in OPEN_STATUSES}
             for r in reversed(rows[-limit:])]  # fmt: skip
    return {
        "enabled": enabled(),
        "per_order": float(PER_ORDER_KRW),
        "daily": float(DAILY_KRW),
        "used_today": float(used_today(rows, now)),
        "fx_buffer": float(FX_BUFFER),
        "max_gap": float(MAX_GAP),
        "orders": shown,
    }
