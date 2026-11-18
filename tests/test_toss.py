import json
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy.orm import Session

from stockmate import portfolio, sync
from stockmate.brokers import toss
from stockmate.brokers.toss import TossClient, TossError
from stockmate.db import make_engine
from stockmate.models import Broker, Market


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(toss, "MIN_CALL_INTERVAL", 0)


@pytest.fixture
def session():
    with Session(make_engine("sqlite://")) as s:
        yield s


HOLDINGS = {
    "result": {
        "items": [
            {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "currency": "KRW",
             "quantity": "10", "averagePurchasePrice": "70000", "lastPrice": "274500"},
            {"symbol": "nvda", "name": "엔비디아", "marketCountry": "US", "currency": "USD",
             "quantity": "0.053003", "averagePurchasePrice": "150.1781", "lastPrice": "180"},
            {"symbol": "7203", "name": "도요타", "marketCountry": "JP", "currency": "JPY",
             "quantity": "1", "averagePurchasePrice": "3000", "lastPrice": "3100"},
        ]
    }
}  # fmt: skip


class FakeToss:
    """토스 API 흉내. 받은 요청을 기록한다."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.revoke_next = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth2/token":
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"t{self.tokens_issued}", "token_type": "Bearer", "expires_in": 86400}
            )
        if self.revoke_next:
            self.revoke_next = False
            return httpx.Response(401, json={"error": {"code": "token-revoked", "message": "무효"}})
        if path == "/api/v1/accounts":
            return httpx.Response(
                200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]}
            )
        if path == "/api/v1/holdings":
            assert request.headers["X-Tossinvest-Account"] == "7"
            return httpx.Response(200, json=HOLDINGS)
        if path == "/api/v1/buying-power":
            amount = {"KRW": "100000", "USD": "10.5"}[request.url.params["currency"]]
            return httpx.Response(200, json={"result": {"currency": "X", "cashBuyingPower": amount}})
        if path == "/api/v1/exchange-rate":
            return httpx.Response(200, json={"result": {"rate": "1410", "midRate": "1400"}})
        return httpx.Response(404, json={"error": {"code": "edge-blocked"}})


def make_client(tmp_path, fake):
    return TossClient("id", "secret", tmp_path / "token.json", transport=httpx.MockTransport(fake))


def test_sync_imports_holdings_with_avg_cost_and_cash(session, tmp_path):
    fake = FakeToss()
    result = sync.sync_toss(session, client=make_client(tmp_path, fake))

    account = result.account
    assert account.broker is Broker.TOSS and result.created
    held = {h.instrument.symbol: h for h in account.holdings}
    assert set(held) == {"005930", "NVDA"}  # 일본 종목은 건너뛴다
    assert held["NVDA"].instrument.market is Market.US
    assert held["NVDA"].quantity == D("0.053003") and held["NVDA"].avg_cost == D("150.1781")
    assert held["005930"].avg_cost == D(70000)
    assert held["NVDA"].avg_fx == D(1400) and held["005930"].avg_fx is None  # 새 미국 종목은 지금 환율
    assert account.cash == D(100000) and account.cash_usd == D("10.5")  # 원화·달러를 따로 둔다
    assert "도요타" in result.warnings[0]
    assert (result.domestic, result.overseas) == (1, 1)


def test_sync_replaces_sold_holdings(session, tmp_path):
    account = portfolio.create_account(session, "토스", Broker.TOSS)
    sold = portfolio.get_or_create_instrument(session, "QLD")
    portfolio.set_holding(session, account, sold, D(1), D(100))

    result = sync.sync_toss(session, client=make_client(tmp_path, FakeToss()))
    assert result.removed == ["QLD"]


def test_only_read_requests_are_sent(tmp_path):
    fake = FakeToss()
    client = make_client(tmp_path, fake)
    client.balance()
    assert {r.method for r in fake.requests if r.url.path != "/oauth2/token"} == {"GET"}
    blocked = [
        ("GET", "/api/v1/orders"),
        ("POST", "/api/v1/conditional-orders"),
        ("POST", "/api/v1/orders/abc/modify"),
        ("POST", "/api/v1/orders/../oauth2"),
        ("DELETE", "/api/v1/orders/abc"),
    ]
    for method, path in blocked:
        with pytest.raises(TossError, match="보낼 수 없는"):
            client._call(method, path)


def test_limit_order_request(tmp_path):
    sent = []

    def fake(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": 60_000})
        sent.append(request)
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"result": {"orderId": "o1", "clientOrderId": "c1"}})
        return httpx.Response(200, json={"result": {"orderId": "x"}})

    client = TossClient("id", "s", tmp_path / "t.json", account_seq=7, transport=httpx.MockTransport(fake))
    assert client.place_limit_order("c1", "005930", "BUY", 3, D("70000")) == "o1"
    body = json.loads(sent[0].content)
    assert body == {"clientOrderId": "c1", "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
                    "timeInForce": "DAY", "quantity": "3", "price": "70000"}  # fmt: skip
    assert sent[0].headers["X-Tossinvest-Account"] == "7"
    client.cancel_order("o1")
    assert (sent[1].method, sent[1].url.path) == ("POST", "/api/v1/orders/o1/cancel")


def test_token_is_cached_and_renewed_once_when_revoked(tmp_path):
    fake = FakeToss()
    client = make_client(tmp_path, fake)
    client.account_seq()
    assert fake.tokens_issued == 1

    # 새 클라이언트도 파일에 캐시된 토큰을 쓴다 (토큰은 키당 1개만 유효해서 함부로 새로 받으면 안 된다)
    again = make_client(tmp_path, fake)
    fake.revoke_next = True
    again.account_seq()
    assert fake.tokens_issued == 2
    assert json.loads((tmp_path / "token.json").read_text())["access_token"] == "t2"


def test_ip_block_has_helpful_message(tmp_path):
    def blocked(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": 60_000})
        return httpx.Response(403, json={"error": {"code": "edge-blocked", "message": ""}})

    client = TossClient("id", "secret", tmp_path / "t.json", transport=httpx.MockTransport(blocked))
    with pytest.raises(TossError, match="허용 IP"):
        client.account_seq()


def test_sync_keeps_purchase_fx_when_quantity_is_unchanged(session, tmp_path):
    account = portfolio.create_account(session, "토스", Broker.TOSS)
    nvda = portfolio.get_or_create_instrument(session, "NVDA")
    # 스크린샷으로 넣었던 값: 평단을 원화 원금 / 화면 환율(1357.6)로 역산해 둔 상태
    portfolio.set_holding(session, account, nvda, D("0.053003"), D("160"), D("1357.6"))

    sync.sync_toss(session, client=make_client(tmp_path, FakeToss()))
    holding = next(h for h in account.holdings if h.instrument.symbol == "NVDA")
    assert holding.avg_cost == D("150.1781")
    # 원화 원금(0.053003 × 160 × 1357.6)이 그대로 남도록 매입 환율을 다시 계산한다
    assert abs(holding.quantity * holding.avg_cost * holding.avg_fx - D("0.053003") * 160 * D("1357.6")) < D("0.01")


def test_order_busy_conflict_is_retried_and_errors_carry_status(tmp_path, monkeypatch):
    monkeypatch.setattr(toss.time, "sleep", lambda s: None)
    calls = []

    def fake(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": 60_000})
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(409, json={"error": {"code": "request-in-progress", "message": "처리 중"}})
        if len(calls) == 2:
            return httpx.Response(200, json={"result": {"orderId": "o1"}})
        return httpx.Response(422, json={"error": {"code": "market-closed", "message": "장 마감"}})

    client = TossClient("id", "s", tmp_path / "t.json", account_seq=7, transport=httpx.MockTransport(fake))
    assert client.place_limit_order("c1", "005930", "BUY", 1, D(70000)) == "o1"
    assert json.loads(calls[0].content)["clientOrderId"] == json.loads(calls[1].content)["clientOrderId"]
    with pytest.raises(TossError) as e:
        client.place_limit_order("c2", "005930", "BUY", 1, D(70000))
    assert (e.value.status, e.value.code) == (422, "market-closed") and "주문을 받지 않았어요" in str(e.value)
