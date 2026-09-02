from autonomy_orchestrator.learning_view import build_learning_curve


def test_learning_curve_is_chronological_and_surfaces_progress() -> None:
    newest_first = [
        {
            "report": {
                "campaign_id": "new",
                "as_of": "2026-02-01T00:00:00+00:00",
                "status": "RECORDED",
                "strategy_id": "ema_rsi_macd",
                "base_version": "v1",
                "symbol": "AAPL",
                "paper_feedback": {
                    "trades": 42,
                    "realized_total": 180.0,
                    "sharpe": 0.7,
                    "sharpe_annualised": 1.1,
                    "win_rate": 0.57,
                    "max_drawdown_amount": 35.0,
                },
                "campaign": {
                    "challengers_evaluated": 4,
                    "pooled_trials": 80,
                    "results": [
                        {
                            "deflated_sharpe_campaign": 0.97,
                            "out_of_sample_sharpe": 1.3,
                        }
                    ],
                },
                "qualified_proposals": ["chal-0123456789ab"],
                "content_hash": "b" * 64,
            }
        },
        {
            "report": {
                "campaign_id": "old",
                "as_of": "2026-01-01T00:00:00+00:00",
                "status": "INSUFFICIENT_PAPER_EVIDENCE",
                "strategy_id": "ema_rsi_macd",
                "base_version": "v1",
                "symbol": "AAPL",
                "paper_feedback": {"trades": 8, "realized_total": -20.0},
                "campaign": None,
                "qualified_proposals": [],
                "content_hash": "a" * 64,
            }
        },
    ]

    points = build_learning_curve(newest_first)

    assert [point["campaign_id"] for point in points] == ["old", "new"]
    assert points[0]["paper_round_trips"] == 8
    assert points[1]["paper_round_trips"] == 42
    assert points[1]["qualified_proposals"] == 1
    assert points[1]["best_deflated_sharpe"] == 0.97
    assert points[1]["best_out_of_sample_sharpe"] == 1.3


def test_learning_curve_ignores_unreadable_rows() -> None:
    assert build_learning_curve([{"report": None}, {}]) == []
