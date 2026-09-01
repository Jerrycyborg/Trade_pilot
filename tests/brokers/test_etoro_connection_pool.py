"""The broker reuses one connection pool across requests."""

from __future__ import annotations

from brokers.etoro_broker import EtoroBroker


class Response:
    content = b"{}"

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {}


def test_etoro_broker_reuses_client_and_can_close(monkeypatch) -> None:
    created = []
    requested = []
    closed = []

    class Client:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def request(self, *args, **kwargs):
            requested.append((args, kwargs))
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr("brokers.etoro_broker.httpx.Client", Client)
    broker = EtoroBroker("public", "user")
    broker._request("GET", "/one")
    broker._request("GET", "/two")
    broker.close()

    assert len(created) == 1
    assert len(requested) == 2
    assert closed == [True]
