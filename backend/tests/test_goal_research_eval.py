"""Goal / Deep Research 离线评测指标测试（确定性纯函数）。"""
from types import SimpleNamespace

from scripts.goal_research_eval import build_metrics, citation_accuracy, render_markdown


def test_goal_research_metrics_from_synthetic_state():
    goals = [
        SimpleNamespace(
            goal_id="g1", run_id="r1", origin_run_id="r1", status="completed",
            objective="调研并交付报告",
            acceptance_criteria=[
                {"id": "AC-001", "required": True, "status": "passed"},
                {"id": "AC-002", "required": False, "status": "failed"},
            ],
            created_at=None, finished_at=None,
        ),
        SimpleNamespace(
            goal_id="g2", run_id="r2", origin_run_id="r2", status="active",
            objective="构建项目",
            acceptance_criteria=[{"id": "AC-001", "required": True, "status": "pending"}],
            created_at=None, finished_at=None,
        ),
    ]
    iterations = [
        SimpleNamespace(goal_id="g1", run_id="r1"),
        SimpleNamespace(goal_id="g2", run_id="r2"),
    ]
    runs = [
        SimpleNamespace(
            id="r1", status="completed", attempt=2,
            summary={"model_calls": 5, "tool_calls": 3, "tokens_generated": 100},
            final_text="结论 https://a.example/x 与 https://b.example/y。存在冲突与局限。",
            created_at=None, finished_at=None,
        ),
        SimpleNamespace(
            id="r2", status="failed", attempt=1, summary={"model_calls": 1},
            final_text="", created_at=None, finished_at=None,
        ),
    ]
    plans = [
        SimpleNamespace(
            research_id="rp1", goal_id="g1", questions=[
                {"id": "RQ-001", "priority": "high", "status": "pending"},
                {"id": "RQ-002", "priority": "medium", "status": "pending"},
            ],
        ),
    ]
    sources = [
        SimpleNamespace(
            research_id="rp1", url="https://a.example/x", status="read",
            published_at="2025-01-01",
        ),
        SimpleNamespace(
            research_id="rp1", url="https://b.example/y", status="discovered",
            published_at=None,
        ),
    ]
    evidence = [
        SimpleNamespace(research_id="rp1", question_id="RQ-001", evidence_id="EV-001"),
    ]
    claims = [
        SimpleNamespace(
            research_id="rp1", status="supported",
            support_evidence=["EV-001"], contradict_evidence=[],
        ),
        SimpleNamespace(
            research_id="rp1", status="conflicting",
            support_evidence=["EV-001"], contradict_evidence=["EV-002"],
        ),
    ]

    metrics = build_metrics(
        goals=goals, iterations=iterations, runs=runs, plans=plans,
        sources=sources, evidence=evidence, claims=claims,
        goal_texts={"g1": [runs[0].final_text]},
    )

    assert metrics["goals"]["completion_rate"] == 0.5
    assert metrics["goals"]["false_completions"] == 0
    assert metrics["goals"]["objective_persisted_rate"] == 1.0
    assert metrics["recovery"]["recovery_success_rate"] == 1.0
    assert metrics["research"]["research_coverage"] == 0.5
    assert metrics["research"]["high_priority_coverage"] == 1.0
    assert metrics["research"]["source_read_rate"] == 0.5
    assert metrics["research"]["claim_support_rate"] == 0.5
    assert metrics["research"]["conflict_detection_rate"] == 1.0
    assert metrics["research"]["conflict_disclosure_rate"] == 1.0
    assert metrics["research"]["citation_accuracy"] == 0.5
    assert metrics["cost"]["model_calls_per_success"] == 5.0

    markdown = render_markdown(metrics)
    assert "Goal Completion Rate" in markdown
    assert "Citation Accuracy" in markdown


def test_citation_accuracy_ignores_unregistered_urls():
    result = citation_accuracy(
        ["见 https://a.example/x 和 https://evil.example/z"],
        {"https://a.example/x"},
    )
    assert result == {"cited": 2, "matched": 1}


def test_recovery_counts_cross_run_resume():
    """暂停后“继续”在新 Run 完成，应计入 Recovery Success。"""
    goals = [SimpleNamespace(
        goal_id="g1", run_id="r2", origin_run_id="r1", status="completed",
        objective="中断后继续的目标",
        acceptance_criteria=[{"id": "AC-001", "required": True, "status": "passed"}],
        created_at=None, finished_at=None,
    )]
    runs = [
        SimpleNamespace(id="r1", status="limit_exceeded", attempt=1,
                        summary={"model_calls": 2}, final_text="", created_at=None,
                        finished_at=None),
        SimpleNamespace(id="r2", status="completed", attempt=1,
                        summary={"model_calls": 3}, final_text="完成。", created_at=None,
                        finished_at=None),
    ]
    metrics = build_metrics(
        goals=goals, iterations=[], runs=runs, plans=[], sources=[],
        evidence=[], claims=[], goal_texts={},
    )
    recovery = metrics["recovery"]
    assert recovery["resumed_goals"] == 1
    assert recovery["recovered_success"] == 1
    assert recovery["recovery_success_rate"] == 1.0
