from autonomy_orchestrator.latency import LatencyBook


def test_latency_book_reports_nearest_rank_percentiles_and_budget() -> None:
    book = LatencyBook(window=20)
    for value in range(1, 21):
        book.record_ms("execution_ack", value)

    stage = book.snapshot({"execution_ack": 19.0})["stages"]["execution_ack"]

    assert stage == {
        "count": 20,
        "last_ms": 20.0,
        "p50_ms": 10.0,
        "p95_ms": 19.0,
        "max_ms": 20.0,
        "budget_ms": 19.0,
        "within_budget": True,
    }


def test_latency_book_keeps_a_bounded_window() -> None:
    book = LatencyBook(window=20)
    for value in range(25):
        book.record_ms("cycle", value)

    stage = book.snapshot()["stages"]["cycle"]

    assert stage["count"] == 20
    assert stage["p50_ms"] == 14.0
    assert stage["max_ms"] == 24.0


def test_latency_book_refuses_invalid_samples() -> None:
    book = LatencyBook()

    book.record_ms("", 1.0)
    book.record_ms("quote", -1.0)
    book.record_ms("quote", float("nan"))

    assert book.snapshot()["stages"] == {}
