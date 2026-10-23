#!/usr/bin/env python3
"""Recover drift-linked transit targets from photometry and repeat logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path


TOP_KEYS = {
    "schema_version",
    "case_id",
    "targets",
    "light_curves",
    "batch_logs",
    "period_bounds_days",
}
TARGET_KEYS = {
    "target_code",
    "depth_ppm",
    "duration_h",
    "stellar_radius_rsun",
}
CURVE_KEYS = {
    "curve_code",
    "batch_reference_d",
    "time_d",
    "flux",
}
LOG_KEYS = {
    "log_code",
    "curve_code",
    "repeat_code",
    "batch_reference_d",
    "recorded_center_d",
    "recorded_depth_ppm",
    "recorded_width_h",
    "anchor_center_d",
}
BOUND_KEYS = {"min", "max"}
HISTORY_KEYS = {"case_id", "fingerprint", "target_code", "orbit_sha256"}


class TransitInputError(ValueError):
    pass


def pairs_guard(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransitInputError("duplicate JSON key")
        result[key] = value
    return result


def exact_keys(value, expected, label):
    if not isinstance(value, dict):
        raise TransitInputError(f"{label} must be an object")
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        raise TransitInputError(f"{label} missing key {missing[0]}")
    if extra:
        raise TransitInputError(f"{label} extra key {extra[0]}")


def text(value, label):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise TransitInputError(f"{label} is not valid text")
    if "/" in value or "\\" in value:
        raise TransitInputError(f"{label} has a path separator")
    return value


def number(value, label, positive=False, nonnegative=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TransitInputError(f"{label} is not numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise TransitInputError(f"{label} is not finite") from exc
    if not math.isfinite(result):
        raise TransitInputError(f"{label} is not finite")
    if positive and result <= 0:
        raise TransitInputError(f"{label} must be positive")
    if nonnegative and result < 0:
        raise TransitInputError(f"{label} must be nonnegative")
    return result


def validate(payload):
    exact_keys(payload, TOP_KEYS, "payload")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise TransitInputError("schema_version must be integer 1")
    text(payload["case_id"], "case_id")
    bounds = payload["period_bounds_days"]
    exact_keys(bounds, BOUND_KEYS, "period_bounds_days")
    lower = number(bounds["min"], "min", positive=True)
    upper = number(bounds["max"], "max", positive=True)
    if lower != 12.0 or upper != 16.0:
        raise TransitInputError("period bounds must be 12.0 and 16.0")
    targets = payload["targets"]
    curves = payload["light_curves"]
    logs = payload["batch_logs"]
    if not isinstance(targets, list) or not isinstance(curves, list) or not isinstance(logs, list):
        raise TransitInputError("targets, light_curves, and batch_logs must be arrays")
    if len(targets) != 5 or len(curves) != 5:
        raise TransitInputError("exactly five targets and curves are required")
    target_codes = set()
    for index, target in enumerate(targets):
        exact_keys(target, TARGET_KEYS, f"targets[{index}]")
        code = text(target["target_code"], "target_code")
        if code in target_codes:
            raise TransitInputError("target_code must be unique")
        target_codes.add(code)
        for key in ("depth_ppm", "duration_h", "stellar_radius_rsun"):
            number(target[key], key, positive=True)
    curve_codes = set()
    for index, curve in enumerate(curves):
        exact_keys(curve, CURVE_KEYS, f"light_curves[{index}]")
        code = text(curve["curve_code"], "curve_code")
        if code in curve_codes:
            raise TransitInputError("curve_code must be unique")
        curve_codes.add(code)
        number(curve["batch_reference_d"], "batch_reference_d", nonnegative=True)
        times = curve["time_d"]
        flux = curve["flux"]
        if not isinstance(times, list) or not isinstance(flux, list):
            raise TransitInputError("time_d and flux must be arrays")
        if len(times) != 60 or len(flux) != 60:
            raise TransitInputError("each curve must contain sixty points")
        values = [number(value, "time_d") for value in times]
        if any(values[i] >= values[i + 1] for i in range(len(values) - 1)):
            raise TransitInputError("time_d must increase strictly")
        for value in flux:
            number(value, "flux", positive=True)
    if len(logs) != 60:
        raise TransitInputError("batch_logs must contain sixty records")
    log_codes = set()
    repeat_rows = {}
    anchor_count = {code: 0 for code in curve_codes}
    curve_log_count = {code: 0 for code in curve_codes}
    for index, row in enumerate(logs):
        exact_keys(row, LOG_KEYS, f"batch_logs[{index}]")
        log_code = text(row["log_code"], "log_code")
        if log_code in log_codes:
            raise TransitInputError("log_code must be unique")
        log_codes.add(log_code)
        curve_code = text(row["curve_code"], "curve_code")
        if curve_code not in curve_codes:
            raise TransitInputError("batch log references an unknown curve")
        curve_log_count[curve_code] += 1
        repeat_code = text(row["repeat_code"], "repeat_code")
        repeat_rows.setdefault((curve_code, repeat_code), []).append(row)
        number(row["batch_reference_d"], "batch_reference_d", nonnegative=True)
        number(row["recorded_center_d"], "recorded_center_d")
        number(row["recorded_depth_ppm"], "recorded_depth_ppm", positive=True)
        number(row["recorded_width_h"], "recorded_width_h", positive=True)
        anchor = row["anchor_center_d"]
        if anchor is not None:
            number(anchor, "anchor_center_d")
            anchor_count[curve_code] += 1
    if any(count != 12 for count in curve_log_count.values()):
        raise TransitInputError("each curve requires twelve batch log records")
    if any(count != 1 for count in anchor_count.values()):
        raise TransitInputError("each curve requires one clock anchor")
    for curve_code in curve_codes:
        groups = [
            rows
            for (code, _), rows in repeat_rows.items()
            if code == curve_code
        ]
        if len(groups) != 4 or any(len(rows) != 3 for rows in groups):
            raise TransitInputError("each curve requires four repeat groups of three")
    return payload


def read_input(path):
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise TransitInputError(f"cannot read input: {exc.strerror}") from exc
    if not raw.endswith(b"\n"):
        raise TransitInputError("input must end with a newline")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_guard)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransitInputError("input is not valid UTF-8 JSON") from exc
    return raw, validate(payload)


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def phase_distance(left, right, period):
    difference = (left - right) % period
    return min(difference, period - difference)


def detect_groups(curve):
    times = curve["time_d"]
    flux = curve["flux"]
    background = percentile(flux, 0.80)
    threshold = background - 0.00030
    groups = []
    current = []
    for index, value in enumerate(flux):
        if value < threshold:
            if current and times[index] - times[current[-1]] > 0.08:
                groups.append(current)
                current = []
            current.append(index)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    result = []
    for group in groups:
        if len(group) < 2:
            continue
        event_times = [times[index] for index in group]
        event_flux = [flux[index] for index in group]
        local_step = statistics.median(
            event_times[index] - event_times[index - 1]
            for index in range(1, len(event_times))
        )
        result.append(
            {
                "local_center": (event_times[0] + event_times[-1]) / 2.0,
                "local_span": event_times[-1] - event_times[0] + local_step,
                "flux": event_flux,
            }
        )
    return background, result


def fit_clock(curve, rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["repeat_code"], []).append(row)
    numerator = 0.0
    denominator = 0.0
    for group in groups.values():
        ordered = sorted(group, key=lambda row: row["batch_reference_d"])
        base = ordered[0]
        for row in ordered[1:]:
            delta_recorded = row["recorded_center_d"] - base["recorded_center_d"]
            delta_batch = row["batch_reference_d"] - base["batch_reference_d"]
            numerator += delta_recorded * delta_batch
            denominator += delta_recorded * delta_recorded
    if denominator == 0.0:
        raise TransitInputError("clock repeats are degenerate")
    rate = -numerator / denominator
    if not 0.94 <= rate <= 1.06:
        raise TransitInputError("clock rate is outside supported interval")
    anchor = next(row for row in rows if row["anchor_center_d"] is not None)
    offset = (
        anchor["anchor_center_d"]
        - rate * anchor["recorded_center_d"]
        - anchor["batch_reference_d"]
    )
    global_groups = []
    for group in groups.values():
        translated = [
            rate * row["recorded_center_d"]
            + offset
            + row["batch_reference_d"]
            for row in group
        ]
        global_groups.append(statistics.median(translated))
    background, local_groups = detect_groups(curve)
    current_centers = [
        rate * group["local_center"] + offset + curve["batch_reference_d"]
        for group in local_groups
    ]
    current_widths = [
        rate * group["local_span"] * 24.0 for group in local_groups
    ]
    event_flux = [value for group in local_groups for value in group["flux"]]
    event_indices = {
        index
        for index, value in enumerate(curve["flux"])
        if value < background - 0.00030
    }
    outside = [
        value
        for index, value in enumerate(curve["flux"])
        if index not in event_indices
    ]
    measured_depth = (
        (statistics.median(outside) - statistics.median(event_flux)) * 1_000_000.0
        if event_flux and outside
        else statistics.median(row["recorded_depth_ppm"] for row in rows)
    )
    duration_h = (
        statistics.median(current_widths)
        if current_widths
        else statistics.median(row["recorded_width_h"] * rate for row in rows)
    )
    return {
        "rate": rate,
        "offset": offset,
        "log_groups": sorted(global_groups),
        "log_rows": rows,
        "log_centers": [
            rate * row["recorded_center_d"] + offset + row["batch_reference_d"]
            for row in rows
        ],
        "current_centers": sorted(current_centers),
        "depth": measured_depth,
        "duration_h": duration_h,
        "global_times": [
            rate * value + offset + curve["batch_reference_d"]
            for value in curve["time_d"]
        ],
    }


def clamp_period(period, lower, upper):
    return max(lower, min(upper, period)) == period


def candidate_periods(curve_data, lower, upper):
    centers = list(curve_data["log_groups"]) + list(curve_data["current_centers"])
    values = []
    for left in range(len(centers)):
        for right in range(left + 1, len(centers)):
            difference = abs(centers[right] - centers[left])
            if difference <= 0:
                continue
            for cycles in range(1, 25):
                period = difference / cycles
                if clamp_period(period, lower, upper):
                    values.append(round(period, 6))
    values.sort()
    clusters = []
    for value in values:
        if not clusters or value - clusters[-1][-1] > 0.003:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    candidates = [sum(group) / len(group) for group in clusters]
    if not candidates:
        raise TransitInputError("no viable period candidates")
    return candidates


def score_period(curve_data, period, duration_h):
    epoch = curve_data["log_groups"][0] % period
    window = duration_h / 48.0 + 0.012
    group_distances = [
        phase_distance(value, epoch, period)
        for value in curve_data["log_groups"]
    ]
    log_distances = [
        phase_distance(value, epoch, period)
        for value in curve_data["log_centers"]
    ]
    current_distances = [
        phase_distance(value, epoch, period)
        for value in curve_data["current_centers"]
    ]
    sample_distances = [
        phase_distance(value, epoch, period)
        for value in curve_data["global_times"]
    ]
    group_support = sum(value <= window for value in group_distances)
    log_support = sum(value <= window for value in log_distances)
    event_support = sum(value <= window for value in current_distances)
    point_support = sum(value <= window for value in sample_distances)
    residual = sum(value * value for value in group_distances if value <= window)
    consistent = (
        group_support == len(curve_data["log_groups"])
        and event_support == len(curve_data["current_centers"])
        and len(curve_data["current_centers"]) <= 2
    )
    key = (
        int(consistent),
        group_support,
        event_support,
        log_support,
        point_support,
        -residual,
    )
    return {
        "period": period,
        "epoch": epoch,
        "key": key,
        "log_support": log_support,
        "point_support": point_support,
        "total_support": log_support + point_support,
    }


def rank_orbits(curve_data, duration_h, lower, upper):
    scored = [
        score_period(curve_data, period, duration_h)
        for period in candidate_periods(curve_data, lower, upper)
    ]
    scored.sort(key=lambda row: row["key"], reverse=True)
    best = scored[0]
    second_support = max(
        (
            row["total_support"]
            for row in scored[1:]
            if abs(row["period"] - best["period"]) > 0.003
        ),
        default=0,
    )
    best = dict(best)
    best["support_margin"] = max(0, best["total_support"] - second_support)
    return best


def hungarian(cost):
    size = len(cost)
    row_potential = [0.0] * (size + 1)
    column_potential = [0.0] * (size + 1)
    matching = [0] * (size + 1)
    previous = [0] * (size + 1)
    for row in range(1, size + 1):
        matching[0] = row
        column0 = 0
        minimum = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column0] = True
            row0 = matching[column0]
            delta = math.inf
            column1 = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                current = (
                    cost[row0 - 1][column - 1]
                    - row_potential[row0]
                    - column_potential[column]
                )
                if current < minimum[column]:
                    minimum[column] = current
                    previous[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(size + 1):
                if used[column]:
                    row_potential[matching[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if matching[column0] == 0:
                break
        while True:
            column1 = previous[column0]
            matching[column0] = matching[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [0] * size
    for column in range(1, size + 1):
        if matching[column]:
            assignment[matching[column] - 1] = column - 1
    return assignment


def calibrate(payload):
    lower = float(payload["period_bounds_days"]["min"])
    upper = float(payload["period_bounds_days"]["max"])
    curves = payload["light_curves"]
    targets = payload["targets"]
    logs = payload["batch_logs"]
    logs_by_curve = {
        curve["curve_code"]: [
            row for row in logs if row["curve_code"] == curve["curve_code"]
        ]
        for curve in curves
    }
    curve_data = [
        fit_clock(curve, logs_by_curve[curve["curve_code"]])
        for curve in curves
    ]
    orbit_cache = {}
    cost = []
    for curve_index, data in enumerate(curve_data):
        row = []
        for target_index, target in enumerate(targets):
            orbit = rank_orbits(
                data,
                float(target["duration_h"]),
                lower,
                upper,
            )
            orbit_cache[(curve_index, target_index)] = orbit
            depth_scale = max(120.0, float(target["depth_ppm"]) * 0.04)
            duration_scale = max(0.16, float(target["duration_h"]) * 0.045)
            row.append(
                ((data["depth"] - target["depth_ppm"]) / depth_scale) ** 2
                + ((data["duration_h"] - target["duration_h"]) / duration_scale) ** 2
            )
        cost.append(row)
    assignment = hungarian(cost)
    catalog = []
    for curve_index, target_index in enumerate(assignment):
        target = targets[target_index]
        curve = curves[curve_index]
        data = curve_data[curve_index]
        orbit = orbit_cache[(curve_index, target_index)]
        period = round(orbit["period"], 6)
        epoch = round(orbit["epoch"] % period, 6)
        centers = [round(value, 6) for value in data["current_centers"]]
        catalog.append(
            {
                "target_code": target["target_code"],
                "curve_code": curve["curve_code"],
                "time_rate": round(data["rate"], 8),
                "time_offset_d": round(data["offset"], 6),
                "transit_count": len(centers),
                "transit_centers_d": centers,
                "period_days": period,
                "epoch_days": epoch,
                "depth_ppm": round(data["depth"], 3),
                "duration_h": round(data["duration_h"], 3),
                "log_support": orbit["log_support"],
                "point_support": orbit["point_support"],
                "support_margin": orbit["support_margin"],
            }
        )
    catalog.sort(key=lambda row: row["target_code"])
    return {
        "schema_version": 1,
        "case_id": payload["case_id"],
        "catalog": catalog,
        "status": "calibrated",
    }


def compact(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def canonical_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def write_atomic(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".transit-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_history(path):
    target = Path(path)
    if not target.exists():
        return []
    raw = target.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise TransitInputError("history must end with a newline")
    rows = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        try:
            row = json.loads(line, object_pairs_hook=pairs_guard)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransitInputError(f"history line {number} is invalid") from exc
        exact_keys(row, HISTORY_KEYS, f"history line {number}")
        rows.append(row)
    return rows


def publish(payload, raw, history_path, report_path):
    report = calibrate(payload)
    fingerprint = hashlib.sha256(raw).hexdigest()
    history = read_history(history_path)
    known = any(
        row["case_id"] == payload["case_id"]
        and row["fingerprint"] == fingerprint
        for row in history
    )
    if not known:
        for row in report["catalog"]:
            orbit_payload = {
                "time_rate": row["time_rate"],
                "time_offset_d": row["time_offset_d"],
                "transit_count": row["transit_count"],
                "transit_centers_d": row["transit_centers_d"],
                "period_days": row["period_days"],
                "epoch_days": row["epoch_days"],
                "depth_ppm": row["depth_ppm"],
            }
            history.append(
                {
                    "case_id": payload["case_id"],
                    "fingerprint": fingerprint,
                    "target_code": row["target_code"],
                    "orbit_sha256": canonical_hash(orbit_payload),
                }
            )
        write_atomic(history_path, b"".join(compact(row) for row in history))
    write_atomic(report_path, compact(report))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--history", required=True)
    args = parser.parse_args(argv)
    old = Path(args.history).read_bytes() if Path(args.history).exists() else b""
    try:
        raw, payload = read_input(args.input)
        publish(payload, raw, args.history, args.report)
    except TransitInputError as exc:
        if Path(args.history).exists() and Path(args.history).read_bytes() != old:
            write_atomic(args.history, old)
        message = str(exc).replace("\r", " ").replace("\n", " ")
        print(f"transit: {message}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"transit: {exc.strerror}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
