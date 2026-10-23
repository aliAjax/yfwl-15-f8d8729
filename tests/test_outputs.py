from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


ENTRY = os.environ.get("TRANSIT_ENTRY", "/app/transit.py")
DATA_DIR = Path(os.environ.get("TRANSIT_DATA_DIR", "/app/data"))
MAIN_CASE = DATA_DIR / "transit_case.json"
GOLDEN = Path(__file__).with_name("golden.json")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def invoke(tmp_path, payload, case_name="payload.json", history=None):
    case = tmp_path / case_name
    case.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    state = history if history is not None else tmp_path / "history.jsonl"
    process = subprocess.run(
        ["python3", ENTRY, "--input", str(case), "--report", str(report), "--history", str(state)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    return process, report, state


def main_payload():
    return load_json(MAIN_CASE)


def test_main_transit_gate(tmp_path):
    """题面：「输入必须恰好有五个目标和五条未标号曲线。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    data = load_json(report)
    golden = load_json(GOLDEN)
    assert process.returncode == 0
    assert [row["target_code"] for row in data["catalog"]] == golden["target_order"]


def test_success_channels_are_empty(tmp_path):
    """题面：「成功时两个输出流均留空。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    assert process.returncode == 0
    assert process.stdout == ""
    assert process.stderr == ""
    assert load_json(report)["case_id"] == "drift-linked-transit-set"


def test_report_top_schema_is_exact(tmp_path):
    """题面：「报告顶层键：schema_version, case_id, catalog, status」"""
    process, report, _ = invoke(tmp_path, main_payload())
    data = load_json(report)
    assert process.returncode == 0
    assert set(data) == {"schema_version", "case_id", "catalog", "status"}


def test_catalog_row_schema_is_exact(tmp_path):
    """题面：「目录项键：target_code, curve_code, time_rate, time_offset_d, transit_count, transit_centers_d, period_days, epoch_days, depth_ppm, duration_h, log_support, point_support, support_margin」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    expected = {
        "target_code", "curve_code", "time_rate", "time_offset_d",
        "transit_count", "transit_centers_d", "period_days", "epoch_days",
        "depth_ppm", "duration_h", "log_support", "point_support",
        "support_margin",
    }
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(set(row) == expected for row in rows)


def test_catalog_sorted_and_calibrated(tmp_path):
    """题面：「`catalog` 按 `target_code` 升序排列，`status` 为 `calibrated`。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    data = load_json(report)
    codes = [row["target_code"] for row in data["catalog"]]
    assert process.returncode == 0
    assert codes == sorted(codes)
    assert data["status"] == "calibrated"


def test_target_mapping_matches_golden(tmp_path):
    """题面：「目标对应按恢复深度和持续时间的全局一一匹配求得。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    actual = {row["target_code"]: row["curve_code"] for row in rows}
    expected = load_json(GOLDEN)["mapping"]
    assert process.returncode == 0
    assert actual == expected


def test_affine_clock_rate_and_offset_are_recovered(tmp_path):
    """题面：「先用每组重复日志消除未知事件时刻，以线性最小二乘恢复每条曲线的时钟率和相位偏移，不使用漂移网格。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    golden = load_json(GOLDEN)
    rates = {row["target_code"]: row["time_rate"] for row in rows}
    offsets = {row["target_code"]: row["time_offset_d"] for row in rows}
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(abs(rates[target] - golden["time_rates"][target]) < 0.00001 for target in rates)
    assert all(abs(offsets[target] - golden["time_offsets"][target]) < 0.002 for target in offsets)


def test_sparse_transit_counts_are_recovered(tmp_path):
    """题面：「每个目标在当前批次可能留下零到两次真实凌日；更早的重复观测留在批次日志中，足以把各曲线放回同一时间基准。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    actual = {row["target_code"]: row["transit_count"] for row in rows}
    expected = load_json(GOLDEN)["transit_counts"]
    assert process.returncode == 0
    assert actual == expected
    assert all(0 <= value <= 2 for value in actual.values())


def test_transit_centers_are_globally_aligned(tmp_path):
    """题面：「把 `time_rate`、`time_offset_d` 与 `batch_reference_d` 代回后，`transit_centers_d` 给出全局中心；列表升序且长度等于 `transit_count`。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    expected = load_json(GOLDEN)["transit_centers"]
    actual = {row["target_code"]: row["transit_centers_d"] for row in rows}
    assert process.returncode == 0
    assert all(len(values) == next(
        row["transit_count"] for row in rows if row["target_code"] == target
    ) for target, values in actual.items())
    assert all(values == sorted(values) for values in actual.values())
    assert all(
        len(actual[target]) == len(expected[target])
        and all(abs(actual[target][index] - expected[target][index]) < 0.01
                for index in range(len(expected[target])))
        for target in expected
    )


def test_periods_match_repeat_event_lattice(tmp_path):
    """题面：「周期候选只从去漂移后的历史事件中心差除以整数一至二十四产生，并保留十二到十六天的值。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    actual = {row["target_code"]: row["period_days"] for row in rows}
    expected = load_json(GOLDEN)["periods"]
    assert process.returncode == 0
    assert all(abs(actual[target] - expected[target]) < 0.01 for target in expected)
    assert all(12.0 <= row["period_days"] <= 16.0 for row in rows)


def test_epochs_are_in_canonical_phase(tmp_path):
    """题面：「`epoch_days` 是首个历史事件中心在周期内的相位，范围为 `[0, period_days)`。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(0 <= row["epoch_days"] < row["period_days"] for row in rows)


def test_depths_and_durations_match_events_or_logs(tmp_path):
    """题面：「当前无凌日的目标从重复日志恢复周期、深度和宽度；当前有一或两次凌日的目标同时用当前事件复核。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    golden = load_json(GOLDEN)
    depths = {row["target_code"]: row["depth_ppm"] for row in rows}
    durations = {row["target_code"]: row["duration_h"] for row in rows}
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(abs(depths[target] - golden["depths"][target]) < 80 for target in depths)
    assert all(abs(durations[target] - golden["durations"][target]) < 0.25 for target in durations)


def test_global_support_is_unique(tmp_path):
    """题面：「`support_margin` 是最佳与次佳不同周期的总支持差，必须为正。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(row["log_support"] == 12 for row in rows)
    assert all(row["support_margin"] > 0 for row in rows)


def test_support_ranking_uses_history_and_current_points(tmp_path):
    """题面：「支持排序：四组历史事件、当前全部凌日和全部 60 个点按相位距离进入窗口；窗口为 `duration_h / 48 + 0.012` 天，排序依据历史观测数、当前点数、凌日数、残差。」"""
    process, report, _ = invoke(tmp_path, main_payload())
    rows = load_json(report)["catalog"]
    assert process.returncode == 0
    assert len(rows) == 5
    assert all(row["log_support"] == 12 for row in rows)
    assert all(row["point_support"] > 0 for row in rows)
    assert all(row["support_margin"] > 0 for row in rows)


def test_history_schema_and_count_are_exact(tmp_path):
    """题面：「首次输入为五个目标各追加一行；同一输入重放时字节不变；同案号换输入时追加一套五行记录；失败输入保持旧字节。」"""
    process, _, history = invoke(tmp_path, main_payload())
    lines = history.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert process.returncode == 0
    assert len(lines) == 5
    assert all(set(row) == {"case_id", "fingerprint", "target_code", "orbit_sha256"} for row in rows)


def test_same_input_keeps_history_bytes(tmp_path):
    """题面：「首次输入为五个目标各追加一行；同一输入重放时字节不变；同案号换输入时追加一套五行记录；失败输入保持旧字节。」"""
    first, _, history = invoke(tmp_path, main_payload())
    before = history.read_bytes()
    second, _, _ = invoke(tmp_path, main_payload(), "payload-b.json", history)
    assert first.returncode == 0
    assert second.returncode == 0
    assert history.read_bytes() == before


def test_changed_input_appends_five_rows(tmp_path):
    """题面：「首次输入为五个目标各追加一行；同一输入重放时字节不变；同案号换输入时追加一套五行记录；失败输入保持旧字节。」"""
    first, _, history = invoke(tmp_path, main_payload())
    payload = main_payload()
    payload["light_curves"][0]["flux"][5] += 0.00001
    second, _, _ = invoke(tmp_path, payload, "payload-b.json", history)
    assert first.returncode == 0
    assert second.returncode == 0
    assert len(history.read_text(encoding="utf-8").splitlines()) == 10


def test_failed_input_preserves_history_bytes(tmp_path):
    """题面：「首次输入为五个目标各追加一行；同一输入重放时字节不变；同案号换输入时追加一套五行记录；失败输入保持旧字节。」"""
    first, _, history = invoke(tmp_path, main_payload())
    before = history.read_bytes()
    payload = main_payload()
    payload["schema_version"] = 2
    second, _, _ = invoke(tmp_path, payload, "payload-b.json", history)
    assert first.returncode == 0
    assert second.returncode == 2
    assert history.read_bytes() == before


def test_history_fingerprint_is_raw_sha256(tmp_path):
    """题面：「历史文件是键集合为 `case_id`、`fingerprint`、`target_code`、`orbit_sha256` 的紧凑 JSONL。」"""
    payload = main_payload()
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    process, _, history = invoke(tmp_path, payload)
    row = json.loads(history.read_text(encoding="utf-8").splitlines()[0])
    assert process.returncode == 0
    assert row["fingerprint"] == hashlib.sha256(raw).hexdigest()


def test_report_and_history_compact_newlines(tmp_path):
    """题面：「报告和历史均以一个换行结束，暂存后一次换名。」"""
    process, report, history = invoke(tmp_path, main_payload())
    report_bytes = report.read_bytes()
    history_bytes = history.read_bytes()
    assert process.returncode == 0
    assert b'": ' not in report_bytes
    assert b'": ' not in history_bytes
    assert report_bytes.endswith(b"\n") and not report_bytes.endswith(b"\n\n")
    assert history_bytes.endswith(b"\n") and not history_bytes.endswith(b"\n\n")


def test_run_finishes_below_public_limit(tmp_path):
    """题面：「整批反演应在八秒内返回；测试进程在十秒时接管终止。」"""
    case = tmp_path / "payload.json"
    case.write_text(
        json.dumps(main_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    process = subprocess.run(
        ["python3", ENTRY, "--input", str(case), "--report", str(tmp_path / "report.json"), "--history", str(tmp_path / "history.jsonl")],
        text=True,
        capture_output=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    assert process.returncode == 0
    assert elapsed < 8


def test_exact_five_curves_are_required(tmp_path):
    """题面：「输入必须恰好有五个目标和五条未标号曲线。」"""
    payload = main_payload()
    payload["targets"] = payload["targets"][:4]
    payload["light_curves"] = payload["light_curves"][:4]
    process, report, history = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()
    assert not history.exists()


def test_duplicate_curve_code_is_rejected(tmp_path):
    """题面：「`target_code` 与 `curve_code` 分别在同层唯一。」"""
    payload = main_payload()
    payload["light_curves"][1]["curve_code"] = payload["light_curves"][0]["curve_code"]
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_missing_target_key_is_rejected(tmp_path):
    """题面：「目标键：target_code, depth_ppm, duration_h, stellar_radius_rsun」"""
    payload = main_payload()
    del payload["targets"][0]["stellar_radius_rsun"]
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_extra_target_key_is_rejected(tmp_path):
    """题面：「额外键即拒绝。」"""
    payload = main_payload()
    payload["targets"][0]["survey_note"] = "sector-a"
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_duplicate_json_key_is_rejected(tmp_path):
    """题面：「同名键不得在同一层重复。」"""
    text = MAIN_CASE.read_text(encoding="utf-8")
    case = tmp_path / "payload.json"
    case.write_text(text.replace('"schema_version":1,', '"schema_version":1,"schema_version":1,', 1), encoding="utf-8")
    process = subprocess.run(
        ["python3", ENTRY, "--input", str(case), "--report", str(tmp_path / "report.json"), "--history", str(tmp_path / "history.jsonl")],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not (tmp_path / "report.json").exists()


def test_boolean_schema_version_is_rejected(tmp_path):
    """题面：「版本必须是数值一，布尔值不算数值。」"""
    payload = main_payload()
    payload["schema_version"] = True
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_nonfinite_flux_is_rejected(tmp_path):
    """题面：「所有数值字段必须有限。」"""
    payload = main_payload()
    payload["light_curves"][0]["flux"][3] = float("nan")
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_curve_shape_is_rejected(tmp_path):
    """题面：「每条曲线有六十个严格递增的时间点及等长流量数组，并对应十二条日志：四个 `repeat_code`，每组三次跨批次重复观测。」"""
    payload = main_payload()
    payload["light_curves"][0]["flux"].pop()
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stderr.startswith("transit: ")
    assert not report.exists()


def test_input_error_protocol_is_closed(tmp_path):
    """题面：「输入错误：标准输出为空；标准错误一行 `transit: `；状态二；无调用栈。」"""
    payload = main_payload()
    payload["schema_version"] = 2
    process, report, _ = invoke(tmp_path, payload)
    assert process.returncode == 2
    assert process.stdout == ""
    assert process.stderr.startswith("transit: ")
    assert process.stderr.count("\n") == 1
    assert not report.exists()
