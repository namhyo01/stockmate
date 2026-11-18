import datetime as dt
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy.orm import Session

from stockmate import orders, plans
from stockmate.brokers.toss import TossError
from stockmate.db import make_engine
from stockmate.models import Market
from test_portfolio import FakePrices

NOW = dt.datetime(2026, 11, 18, 10, 0)
SAMSUNG = (Market.KRX, "005930")


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


@pytest.fixture(autouse=True)
def order_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "DIR", tmp_path)
    notified.clear()
    monkeypatch.setattr(orders, "_notify", lambda title, body: notified.append(title))


notified: list[str] = []


@pytest.fixture
def otp_ok(monkeypatch):
    """OTP 말고 다른 확인을 보는 테스트: 코드는 늘 맞는 것으로."""
    monkeypatch.setattr(orders, "check_otp", lambda code, now=None: None)


class FakeClient:
    def __init__(self, fail: Exception | None = None):
        self.placed: list[tuple] = []
        self.canceled: list[str] = []
        self.status = "PENDING"
        self.fail = fail
        self.order_fail: dict[str, Exception] = {}

    def account_seq(self):
        return 7

    def place_limit_order(self, client_order_id, symbol, side, quantity, price):
        if self.fail:
            raise self.fail
        self.placed.append((symbol, side, quantity, price))
        return f"o{len(self.placed)}"

    def order(self, order_id):
        if order_id in self.order_fail:
            raise self.order_fail[order_id]
        return {"status": self.status, "execution": {"filledQuantity": "3", "averageFilledPrice": "99000"}}

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return "c1"


def add_plan(session, side="buy", account="토스", symbol="005930", name="삼성전자"):
    plans.apply_actions(session, {"add": [{"side": side, "name": name, "symbol": symbol, "account": account}]}, "chat")
    return plans.open_items(session)[-1]


def place(session, item, qty=3, price="100000", otp="000000", prices=None, client=None, now=NOW):
    prices = prices or FakePrices({SAMSUNG: D(100_000)})
    return orders.place(session, item.id, qty, price, otp, prices, client or FakeClient(), now=now)


def code_at(when, shift=0):
    secret = orders._otp()["secret"]
    return orders.totp(secret, int(when.timestamp()) // orders.OTP_STEP + shift)


def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert orders.totp(secret, 59 // 30) == "287082" and orders.totp(secret, 1111111109 // 30) == "081804"


def test_setup_otp_keeps_existing_unless_reset():
    uri = orders.setup_otp()
    assert uri.startswith("otpauth://totp/stockmate?secret=")
    with pytest.raises(orders.OrderError, match="이미 등록"):
        orders.setup_otp()
    assert orders.setup_otp(reset=True) != uri
    assert oct((orders.DIR / "otp.json").stat().st_mode)[-3:] == "600"




def test_no_otp_registered(session):
    with pytest.raises(orders.OrderError, match="등록돼 있지 않아요"):
        place(session, add_plan(session))


def test_places_limit_order_and_records_it(session, otp_ok):
    client = FakeClient()
    rec = place(session, add_plan(session), client=client)
    assert client.placed == [("005930", "BUY", 3, D(100000))]
    assert rec["status"] == "PENDING" and rec["order_id"] == "o1" and rec["amount_krw"] == "300000"
    assert orders.records()[0]["client_order_id"] == rec["client_order_id"]


def test_only_open_toss_plans(session, otp_ok):
    with pytest.raises(orders.OrderError, match="토스 계좌"):
        place(session, add_plan(session, account="한투"))
    item = add_plan(session, symbol="000660", name="SK하이닉스")
    plans.set_status(session, item.id, plans.DONE)
    with pytest.raises(orders.OrderError, match="열린 계획"):
        place(session, item)


def test_price_far_from_market_is_blocked(session, otp_ok):
    with pytest.raises(orders.OrderError, match="비싸게 사는"):
        place(session, add_plan(session), qty=1, price="106000")
    sell = add_plan(session, side="sell")
    with pytest.raises(orders.OrderError, match="싸게 파는"):
        place(session, sell, qty=1, price="94000")
    place(session, sell, qty=1, price="96000")  # 5% 안쪽은 된다
    with pytest.raises(orders.OrderError, match="지금 가격을 몰라서"):
        place(session, add_plan(session, symbol="000660", name="SK하이닉스"), qty=1)




def test_canceled_orders_count_only_filled_part(session, otp_ok):
    rec = place(session, add_plan(session))
    rec["status"], rec["filled_quantity"] = "CANCELED", "1"
    assert orders.counted_krw(rec) == D(100000)




def test_price_units(session, otp_ok):
    with pytest.raises(orders.OrderError, match="원 단위"):
        place(session, add_plan(session), price="99999.5")
    prices = FakePrices({(Market.US, "BRK-B"): D(500)})
    client = FakeClient()
    brk = add_plan(session, symbol="BRK.B", name="버크셔")
    with pytest.raises(orders.OrderError, match="둘째 자리"):
        place(session, brk, qty=1, price="499.123", prices=prices)
    place(session, brk, qty=1, price="499.12", prices=prices, client=client)
    assert client.placed == [("BRK.B", "BUY", 1, D("499.12"))]  # 토스에는 BRK.B로 보낸다




def test_refresh_closes_plan_when_filled_and_cancel(session, otp_ok):
    item = add_plan(session)
    client = FakeClient()
    rec = place(session, item, client=client)
    orders.refresh(session, client, now=NOW)
    assert orders.records()[0]["filled_quantity"] == "3" and item.status == plans.OPEN
    canceled = orders.cancel(rec["client_order_id"], client)
    assert canceled["status"] == "PENDING_CANCEL" and client.canceled == ["o1"]
    client.status = "FILLED"
    orders.refresh(session, client, now=NOW)
    assert item.status == plans.DONE
    with pytest.raises(orders.OrderError, match="이미 끝난"):
        orders.cancel(rec["client_order_id"], client)
    shown = orders.state(orders.records(), NOW)
    assert shown["orders"][0]["status_label"] == "체결" and shown["used_today"] == 300000


def finish(rec, status="FILLED"):
    """앞 주문을 끝난 것으로 (같은 종목에 열린 주문이 있으면 새 주문을 막으므로)."""
    orders._update(rec["client_order_id"], status=status)


def test_otp_code_once_lock_and_notify(session):
    orders.setup_otp()
    item = add_plan(session)
    finish(place(session, item, otp=code_at(NOW), client=FakeClient()))
    with pytest.raises(orders.OrderError, match="이미 쓴"):
        place(session, item, qty=1, otp=code_at(NOW))  # 같은 코드로 두 번은 안 된다
    finish(place(session, item, qty=1, otp=code_at(NOW, 1), now=NOW + dt.timedelta(seconds=5)))  # 시계 차이 30초는 받는다
    wrong = next(c for c in ("123456", "654321", "111111") if c not in {code_at(NOW, k) for k in (-1, 0, 1, 2)})
    for _ in range(orders.OTP_TRIES):
        with pytest.raises(orders.OrderError, match="맞지 않아요"):
            place(session, item, qty=2, otp=wrong)
    assert len(notified) == orders.OTP_TRIES  # 틀릴 때마다 폰으로
    later = NOW + dt.timedelta(minutes=1)
    with pytest.raises(orders.OrderError, match="10분 동안"):
        place(session, item, qty=2, otp=code_at(later), now=later)  # 맞는 코드도 잠긴 동안은 안 된다 (파일에 남아서 재시작해도)
    after = NOW + orders.OTP_LOCK + dt.timedelta(minutes=1)
    place(session, item, qty=2, otp=code_at(after), now=after)


def test_many_wrong_codes_lock_until_unlocked(session):
    orders.setup_otp()
    item = add_plan(session)
    t = NOW
    for i in range(orders.OTP_HARD_TRIES):
        t = NOW + orders.OTP_LOCK * (i // orders.OTP_TRIES) + dt.timedelta(seconds=i)
        wrong = next(c for c in ("123456", "654321", "111111") if c not in {code_at(t, k) for k in (-1, 0, 1)})
        with pytest.raises(orders.OrderError, match="맞지 않아요"):
            place(session, item, otp=wrong, now=t)
    later = t + dt.timedelta(hours=1)
    with pytest.raises(orders.OrderError, match="--unlock"):
        place(session, item, otp=code_at(later), now=later)
    orders.unlock_otp()
    place(session, item, otp=code_at(later), now=later)


def test_invalid_order_does_not_use_up_code(session):
    orders.setup_otp()
    item = add_plan(session)
    with pytest.raises(orders.OrderError, match="비싸게"):
        place(session, item, price="200000", otp=code_at(NOW))
    place(session, item, otp=code_at(NOW))


def test_per_order_and_daily_limits_count_usd_in_won(session, otp_ok):
    nvda = add_plan(session, symbol="NVDA", name="엔비디아")
    prices = FakePrices({(Market.US, "NVDA"): D(180)}, usd_krw=D(1400))
    with pytest.raises(orders.OrderError, match="한 번에"):
        place(session, nvda, qty=4, price="180", prices=prices)  # 4 × 180 × 1400 × 1.01(환전 여유) = 101.8만원
    for i in range(3):
        rec = place(session, nvda, qty=3, price=f"18{i}", prices=prices)  # 약 76만원씩
        assert rec["amount_krw"] == f"{3 * (180 + i) * 1400 * 1.01:.0f}"
        finish(rec)
    with pytest.raises(orders.OrderError, match="24시간 주문 한도"):
        place(session, nvda, qty=3, price="183", prices=prices)
    # 자정이 지나도 24시간 안이면 그대로 (미국 장은 자정을 넘는다)
    assert orders.used_today(orders.records(), NOW + dt.timedelta(hours=23)) > 2_000_000
    assert orders.used_today(orders.records(), NOW + dt.timedelta(hours=24, seconds=1)) == 0


def test_open_order_blocks_same_stock_and_side(session, otp_ok):
    first = add_plan(session)
    rec = place(session, first)
    with pytest.raises(orders.OrderError, match="끝나지 않은 주문"):
        place(session, first, qty=1, now=NOW + dt.timedelta(hours=1))
    # 계획이 새 것으로 바뀌어도 같은 종목·방향이면 막는다
    second = add_plan(session)
    with pytest.raises(orders.OrderError, match="끝나지 않은 주문"):
        place(session, second, qty=1)
    place(session, add_plan(session, side="sell"), qty=1)  # 반대 방향은 된다
    finish(rec, "CANCELED")
    place(session, second, qty=1)


def test_no_response_still_counts_blocks_and_can_be_resolved(session, otp_ok):
    item = add_plan(session)
    with pytest.raises(orders.OrderError, match="토스 앱에서 꼭 확인"):
        place(session, item, client=FakeClient(fail=httpx.ReadTimeout("x")))
    [rec] = orders.records()
    assert rec["status"] == "UNKNOWN" and orders.used_today([rec], NOW) == D(300000)
    with pytest.raises(orders.OrderError, match="확인 필요"):
        place(session, item)  # 들어갔을 수도 있으니 같은 주문을 또 내지 않게
    with pytest.raises(orders.OrderError, match="주문 번호를 몰라서"):
        orders.cancel(rec["client_order_id"], FakeClient())
    assert orders.resolve(rec["client_order_id"], placed=False)["status"] == "REJECTED"
    assert orders.used_today(orders.records(), NOW) == 0
    with pytest.raises(orders.OrderError, match="확인이 필요한"):
        orders.resolve(rec["client_order_id"], placed=True)


@pytest.mark.parametrize(("status", "code", "result"), [
    (400, "invalid-request", "REJECTED"), (422, "market-closed", "REJECTED"), (429, "rate-limited", "REJECTED"),
    (409, "opposite-pending-order-exists", "REJECTED"), (409, "request-in-progress", "UNKNOWN"),
    (500, "internal-error", "UNKNOWN"), (None, None, "UNKNOWN"),
])  # fmt: skip
def test_only_clear_refusals_are_rejected(session, otp_ok, status, code, result):
    with pytest.raises((orders.OrderError, TossError)):
        place(session, add_plan(session), client=FakeClient(fail=TossError("x", status, code)))
    [rec] = orders.records()
    assert rec["status"] == result
    assert orders.used_today([rec], NOW) == (0 if result == "REJECTED" else D(300000))


def test_unparsable_reply_is_unknown(session, otp_ok):
    with pytest.raises(orders.OrderError, match="답을 받지 못했어요"):
        place(session, add_plan(session), client=FakeClient(fail=KeyError("result")))
    assert orders.records()[0]["status"] == "UNKNOWN"


def test_refresh_keeps_going_after_one_failure(session, otp_ok):
    client = FakeClient()
    a = place(session, add_plan(session), client=client)
    b = place(session, add_plan(session, symbol="000660", name="SK하이닉스"), qty=1, client=client,
              prices=FakePrices({(Market.KRX, "000660"): D(100_000)}))  # fmt: skip
    client.order_fail[a["order_id"]] = TossError("없는 주문", 404, "order-not-found")
    client.status = "FILLED"
    errors = orders.refresh(session, client, now=NOW)
    assert errors == ["삼성전자: 없는 주문"]
    assert {r["client_order_id"]: r["status"] for r in orders.records()} == {a["client_order_id"]: "PENDING", b["client_order_id"]: "FILLED"}


def test_refresh_does_not_overwrite_cancel_in_between(session, otp_ok):
    rec = place(session, add_plan(session))
    orders._update(rec["client_order_id"], status="PENDING_CANCEL")
    assert orders._update(rec["client_order_id"], expect="PENDING", status="PENDING")["status"] == "PENDING_CANCEL"
