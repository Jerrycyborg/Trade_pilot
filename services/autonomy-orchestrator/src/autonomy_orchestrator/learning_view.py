"""Read models for the paper-learning audit trail."""

from __future__ import annotations

from typing import Any


def build_learning_curve(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten immutable cycle reports into chronological learning points."""
    points: list[dict[str, Any]] = []
    for row in reversed(cycles):
        report = row.get("report")
        if not isinstance(report, dict):
            continue
        feedback = report.get("paper_feedback")
        feedback = feedback if isinstance(feedback, dict) else {}
        campaign = report.get("campaign")
        campaign = campaign if isinstance(campaign, dict) else {}
        results = campaign.get("results")
        results = results if isinstance(results, list) else []
        deflated = [
            float(result["deflated_sharpe_campaign"])
            for result in results
            if isinstance(result, dict) and result.get("deflated_sharpe_campaign") is not None
        ]
        out_of_sample = [
            float(result["out_of_sample_sharpe"])
            for result in results
            if isinstance(result, dict) and result.get("out_of_sample_sharpe") is not None
        ]
        qualified = report.get("qualified_proposals")
        qualified = qualified if isinstance(qualified, list) else []
        points.append(
            {
                "campaign_id": report.get("campaign_id"),
                "as_of": report.get("as_of"),
                "status": report.get("status"),
                "strategy_id": report.get("strategy_id"),
                "base_version": report.get("base_version"),
                "symbol": report.get("symbol"),
                "paper_round_trips": int(feedback.get("trades") or 0),
                "paper_realized_total": feedback.get("realized_total"),
                "paper_sharpe_per_trade": feedback.get("sharpe"),
                "paper_sharpe_annualised": feedback.get("sharpe_annualised"),
                "paper_win_rate": feedback.get("win_rate"),
                "paper_max_drawdown_amount": feedback.get("max_drawdown_amount"),
                "challengers_evaluated": int(campaign.get("challengers_evaluated") or 0),
                "pooled_trials": int(campaign.get("pooled_trials") or 0),
                "qualified_proposals": len(qualified),
                "best_deflated_sharpe": max(deflated) if deflated else None,
                "best_out_of_sample_sharpe": (max(out_of_sample) if out_of_sample else None),
                "content_hash": report.get("content_hash"),
            }
        )
    return points
