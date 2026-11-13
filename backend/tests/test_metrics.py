"""P1-05：Prometheus 指标注册表与记录（任务书 §5.6 清单校验）。

运行：
    pytest backend/tests/test_metrics.py -q

注意：指标为进程级累计，断言使用唯一名或 >= 比较，兼容全量运行的累计值。
"""
import re

from app.observability import metrics as m

# 任务书 §5.6 要求的指标名
REQUIRED_METRICS = [
    "agent_runs_total",
    "agent_run_duration_seconds",
    "agent_queue_wait_seconds",
    "agent_active_runs",
    "agent_run_retries_total",
    "agent_recovery_total",
    "agent_tool_calls_total",
    "agent_tool_duration_seconds",
    "agent_model_calls_total",
    "agent_model_latency_seconds",
    "agent_tokens_total",
    "agent_sse_reconnect_total",
    "agent_event_lag_seconds",
    "agent_checkpoint_write_seconds",
    "agent_lease_lost_total",
]


def _render_text() -> str:
    body, _ctype = m.render()
    return body.decode("utf-8")


def _counter_value(text: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)} ([0-9.e+]+)$", text, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


def test_all_required_metrics_registered():
    text = _render_text()
    missing = [name for name in REQUIRED_METRICS if f"# HELP {name}" not in text]
    assert not missing, f"缺少任务书要求的指标: {missing}"


def test_record_calls_render_values():
    # 唯一标签值：避免与其他测试的累计值混淆
    m.observe_run_finished("metric_probe_done", 1.5, agent_type="assistant")
    m.observe_queue_wait(0.2)
    m.set_active_runs(3)
    m.inc_retry("metric_probe_reason")
    m.inc_recovery("metric_probe_decision")
    before_lease = _counter_value(_render_text(), "agent_lease_lost_total")
    m.inc_lease_lost()
    m.observe_tool("metric_probe_tool", "completed", 0.01)
    m.observe_model("platform", "metric_probe_model", "completed", 0.5,
                    prompt_tokens=10, completion_tokens=5)
    m.inc_sse_reconnect()
    m.observe_event_lag(0.01)
    m.observe_checkpoint_write(0.02)

    text = _render_text()
    assert 'agent_runs_total{agent_type="assistant",status="metric_probe_done"} 1.0' in text
    assert "agent_active_runs 3.0" in text
    assert 'agent_tool_calls_total{status="completed",tool="metric_probe_tool"} 1.0' in text
    assert 'agent_tokens_total{direction="output",model="metric_probe_model",provider="platform"} 5.0' in text
    assert 'agent_run_retries_total{reason="metric_probe_reason"} 1.0' in text
    assert 'agent_recovery_total{decision="metric_probe_decision"} 1.0' in text
    assert _counter_value(text, "agent_lease_lost_total") >= before_lease + 1
    assert _counter_value(text, "agent_sse_reconnect_total") >= 1.0
    assert "agent_queue_wait_seconds_count" in text
    assert "agent_checkpoint_write_seconds_count" in text
    assert "agent_event_lag_seconds_count" in text


def test_recording_never_raises_on_bad_values():
    """指标记录防御式：负值截断为 0，缺省维度正常记录，不抛异常。"""
    m.observe_queue_wait(-5)
    m.observe_tool("t", "completed", None)
    m.observe_model("p", "m", "failed")
