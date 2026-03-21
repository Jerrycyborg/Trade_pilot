from contracts import ExecutionOrderRequest, OrderStatus

from brokers.etoro_broker import EtoroBroker


def test_etoro_broker_place_order_with_mocked_requests(monkeypatch) -> None:
    broker = EtoroBroker(api_key="pub", user_key="user", demo=True)

    def fake_request(method: str, path: str, **kwargs):
        if path == "/market-data/search":
            return {"items": [{"instrumentId": 42, "internalSymbolFull": "AAPL"}]}
        if path.endswith("market-open-orders/by-amount"):
            return {"orderId": "ord-123", "fillPrice": 189.5}
        raise AssertionError(path)

    monkeypatch.setattr(broker, "_request", fake_request)

    result = broker.place_order(
        ExecutionOrderRequest(
            signal_id="sig-1",
            symbol="AAPL",
            side="BUY",
            qty=10,
            order_type="MARKET",
            time_in_force="DAY",
        )
    )

    assert result.status == OrderStatus.ACCEPTED
    assert result.external_order_id == "ord-123"
    assert result.fill_price == 189.5
