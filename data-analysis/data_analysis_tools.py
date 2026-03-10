"""
MCP stdio server providing time-series analysis tools for HomeAssistant sensor history data.

Input data format (all tools): JSON string — array of {state, last_changed} objects,
exactly as returned by get_entity_history with minimal_response=true:
  [{"state": "0.0012", "last_changed": "2024-03-10T08:00:00+00:00"}, ...]

Tools:
  - analyze_timeseries:     Statistics overview + data type detection (start here)
  - detect_active_periods:  Find windows when a value is "active" above a threshold
  - detect_anomalies:       Find values outside the rolling baseline (sigma-based)
  - find_daily_patterns:    Compute hourly/day-of-week activity distribution
  - generate_chart:         Produce a base64-encoded PNG visualization
"""

import asyncio
import base64
import io
import json

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from scipy import stats as scipy_stats

matplotlib.use("Agg")

server = Server("data-analysis-tools")


def _parse_history(data_json: str) -> pd.DataFrame:
    """Parse HA history JSON into a DataFrame with a numeric 'value' column where possible."""
    records = json.loads(data_json)
    if not records:
        return pd.DataFrame(columns=["timestamp", "state"])

    df = pd.DataFrame(records)
    df = df.rename(columns={"last_changed": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["value"] = pd.to_numeric(df["state"], errors="coerce")
    return df


def _is_numeric(df: pd.DataFrame) -> bool:
    return df["value"].notna().mean() >= 0.8


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="analyze_timeseries",
            description=(
                "Compute a statistics overview of HomeAssistant entity history data. "
                "Detects whether the data is numeric (sensor readings) or categorical "
                "(on/off states). Returns descriptive stats for numeric data, or a state "
                "distribution and transition count for categorical data. "
                "Always call this first — the pattern_hint field in the result tells you "
                "which deeper analysis tool to use next."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": (
                            "JSON string — array of {state, last_changed} objects "
                            "as returned by get_entity_history."
                        ),
                    },
                    "entity_label": {
                        "type": "string",
                        "description": "Optional label for the entity (e.g. 'laundry vibration x-axis'). Used in output only.",
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="detect_active_periods",
            description=(
                "Find time windows when a numeric sensor value was 'active' — i.e. above a "
                "threshold. Useful for detecting when an appliance (washer, dryer, pump, TV) "
                "was running. The threshold can be auto-detected via Otsu's method if not "
                "provided. Short spikes and brief gaps can be filtered out. "
                "Returns a list of detected runs with start/end/peak/duration, plus a "
                "recommended_threshold value that can be used to build a HA automation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "JSON string — array of {state, last_changed} objects.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "Value above which the sensor is considered 'active'. "
                            "Omit to auto-detect using Otsu's method."
                        ),
                    },
                    "min_duration_seconds": {
                        "type": "number",
                        "description": "Minimum run duration in seconds. Shorter runs are ignored. Default: 120.",
                    },
                    "merge_gap_seconds": {
                        "type": "number",
                        "description": (
                            "If two active periods are separated by less than this many seconds, "
                            "merge them into one. Default: 60."
                        ),
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="detect_anomalies",
            description=(
                "Find time periods where a numeric sensor value deviated significantly from "
                "its rolling baseline. Useful for detecting broken sensors, unusual temperature "
                "spikes, unexpected usage events, or hardware faults. "
                "Uses a rolling z-score approach: points more than sigma_threshold standard "
                "deviations from the rolling mean are flagged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "JSON string — array of {state, last_changed} objects.",
                    },
                    "sigma_threshold": {
                        "type": "number",
                        "description": "Number of standard deviations from rolling mean to flag as anomalous. Default: 3.0.",
                    },
                    "rolling_window_hours": {
                        "type": "number",
                        "description": "Rolling window size in hours for computing the local baseline. Default: 168 (7 days).",
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="find_daily_patterns",
            description=(
                "Discover typical time-of-day and day-of-week usage patterns from sensor history. "
                "For numeric data, computes the rolling mean per hour-of-day. For categorical data "
                "(on/off), computes the fraction of time the entity is 'on' per hour. "
                "Returns hourly_averages (24 values), peak_hours, day_of_week_variation, "
                "and a dominant_pattern description."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "JSON string — array of {state, last_changed} objects.",
                    },
                },
                "required": ["data"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="generate_chart",
            description=(
                "Generate a PNG chart from sensor history data. "
                "Returns a base64-encoded PNG image (base64_png field) and a suggested artifact name. "
                "After calling this tool, save the result as an artifact using save_artifact with "
                "content_type='image/png' and the suggested_artifact_name.\n"
                "Chart types:\n"
                "  timeseries   — line plot of value over time (use with threshold to draw a reference line)\n"
                "  histogram    — value frequency distribution\n"
                "  heatmap      — hour-of-day × day-of-week activity grid\n"
                "  daily_pattern — bar chart of average value or on-fraction by hour of day"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "JSON string — array of {state, last_changed} objects.",
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["timeseries", "histogram", "heatmap", "daily_pattern"],
                        "description": "Type of chart to generate.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Chart title.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Optional reference line drawn on timeseries charts (e.g. the active threshold).",
                    },
                },
                "required": ["data", "chart_type", "title"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "analyze_timeseries":
            result = _analyze_timeseries(arguments)
        elif name == "detect_active_periods":
            result = _detect_active_periods(arguments)
        elif name == "detect_anomalies":
            result = _detect_anomalies(arguments)
        elif name == "find_daily_patterns":
            result = _find_daily_patterns(arguments)
        elif name == "generate_chart":
            result = _generate_chart(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        return [types.TextContent(type="text", text=json.dumps({"error": str(exc)}))]


# ── Tool implementations ──────────────────────────────────────────────────────

def _analyze_timeseries(args: dict) -> dict:
    df = _parse_history(args["data"])
    label = args.get("entity_label", "entity")

    if df.empty:
        return {"error": "No data points found in the provided history."}

    total = len(df)
    span_hours = (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 3600

    if _is_numeric(df):
        vals = df["value"].dropna()
        result = {
            "entity_label": label,
            "dtype": "numeric",
            "count": int(total),
            "span_hours": round(span_hours, 1),
            "min": round(float(vals.min()), 4),
            "max": round(float(vals.max()), 4),
            "mean": round(float(vals.mean()), 4),
            "std": round(float(vals.std()), 4),
            "p25": round(float(vals.quantile(0.25)), 4),
            "p50": round(float(vals.quantile(0.50)), 4),
            "p75": round(float(vals.quantile(0.75)), 4),
            "p95": round(float(vals.quantile(0.95)), 4),
        }
        value_range = float(vals.max()) - float(vals.min())
        if value_range > float(vals.mean()) * 0.5 and float(vals.mean()) > 0:
            result["pattern_hint"] = (
                "Wide value range detected — use detect_active_periods to find when this "
                "sensor was active, or detect_anomalies to find unusual spikes."
            )
        else:
            result["pattern_hint"] = (
                "Use find_daily_patterns to discover typical time-of-day usage, or "
                "detect_anomalies to find values outside the normal range."
            )
    else:
        state_counts = df["state"].value_counts()
        transitions = int((df["state"] != df["state"].shift()).sum()) - 1
        result = {
            "entity_label": label,
            "dtype": "categorical",
            "count": int(total),
            "span_hours": round(span_hours, 1),
            "state_distribution": {str(k): int(v) for k, v in state_counts.items()},
            "transition_count": max(0, transitions),
            "pattern_hint": (
                "Categorical data detected — use find_daily_patterns to discover when "
                "this entity is typically active, or generate_chart with chart_type='heatmap' "
                "for a visual overview."
            ),
        }

    return result


def _otsu_threshold(values: np.ndarray) -> float:
    """Compute Otsu's method threshold on 1-D float array."""
    counts, bin_edges = np.histogram(values, bins=256)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    total = counts.sum()
    if total == 0:
        return float(np.median(values))

    best_thresh = float(np.median(values))
    best_var = 0.0
    sum_all = float((counts * bin_centers).sum())
    w_b = 0.0
    sum_b = 0.0

    for i, count in enumerate(counts):
        w_b += count
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += bin_centers[i] * count
        mean_b = sum_b / w_b
        mean_f = (sum_all - sum_b) / w_f
        var_between = w_b * w_f * (mean_b - mean_f) ** 2
        if var_between > best_var:
            best_var = var_between
            best_thresh = bin_centers[i]

    return float(best_thresh)


def _detect_active_periods(args: dict) -> dict:
    df = _parse_history(args["data"])
    if df.empty or not _is_numeric(df):
        return {"error": "detect_active_periods requires numeric sensor data."}

    min_duration = float(args.get("min_duration_seconds", 120))
    merge_gap = float(args.get("merge_gap_seconds", 60))

    vals = df["value"].dropna()
    baseline = float(vals.quantile(0.10))

    if "threshold" in args:
        threshold = float(args["threshold"])
    else:
        threshold = _otsu_threshold(vals.values)

    df["active"] = df["value"].fillna(0) > threshold

    # Build raw segments
    segments = []
    in_active = False
    seg_start = None
    seg_peak = None

    for _, row in df.iterrows():
        if row["active"] and not in_active:
            in_active = True
            seg_start = row["timestamp"]
            seg_peak = row["value"]
        elif row["active"] and in_active:
            seg_peak = max(seg_peak, row["value"])
        elif not row["active"] and in_active:
            in_active = False
            segments.append((seg_start, row["timestamp"], seg_peak))

    if in_active and seg_start is not None:
        segments.append((seg_start, df["timestamp"].iloc[-1], seg_peak))

    # Merge close segments
    if segments:
        merged = [list(segments[0])]
        for start, end, peak in segments[1:]:
            gap = (start - merged[-1][1]).total_seconds()
            if gap <= merge_gap:
                merged[-1][1] = end
                merged[-1][2] = max(merged[-1][2], peak)
            else:
                merged.append([start, end, peak])
        segments = [(s, e, p) for s, e, p in merged]

    # Filter by minimum duration
    runs = []
    for start, end, peak in segments:
        duration_s = (end - start).total_seconds()
        if duration_s >= min_duration:
            runs.append({
                "start": start.isoformat(),
                "end": end.isoformat(),
                "peak_value": round(float(peak), 4),
                "duration_minutes": round(duration_s / 60, 1),
            })

    total_active_hours = sum(r["duration_minutes"] for r in runs) / 60

    return {
        "detected_runs": runs,
        "run_count": len(runs),
        "recommended_threshold": round(threshold, 4),
        "baseline_value": round(baseline, 4),
        "total_active_hours": round(total_active_hours, 2),
    }


def _detect_anomalies(args: dict) -> dict:
    df = _parse_history(args["data"])
    if df.empty or not _is_numeric(df):
        return {"error": "detect_anomalies requires numeric sensor data."}

    sigma = float(args.get("sigma_threshold", 3.0))
    window_hours = float(args.get("rolling_window_hours", 168))

    df = df.set_index("timestamp").sort_index()
    window = f"{int(window_hours)}h"

    rolling_mean = df["value"].rolling(window, min_periods=10).mean()
    rolling_std = df["value"].rolling(window, min_periods=10).std()

    z_scores = (df["value"] - rolling_mean) / rolling_std.replace(0, np.nan)
    anomalous = z_scores.abs() > sigma

    anomalous_periods = []
    in_anomaly = False
    a_start = None
    a_baseline = None
    a_actual = None
    a_severity = None

    for ts, is_anom in anomalous.items():
        if is_anom and not in_anomaly:
            in_anomaly = True
            a_start = ts
            a_baseline = float(rolling_mean[ts]) if not np.isnan(rolling_mean[ts]) else None
            a_actual = float(df["value"][ts])
            a_severity = float(abs(z_scores[ts]))
        elif is_anom and in_anomaly:
            a_actual = float(df["value"][ts])
            a_severity = max(a_severity, float(abs(z_scores[ts])))
        elif not is_anom and in_anomaly:
            in_anomaly = False
            anomalous_periods.append({
                "start": a_start.isoformat(),
                "end": ts.isoformat(),
                "severity": round(a_severity, 2),
                "baseline_value": round(a_baseline, 4) if a_baseline is not None else None,
                "actual_value": round(a_actual, 4),
            })

    if in_anomaly and a_start is not None:
        anomalous_periods.append({
            "start": a_start.isoformat(),
            "end": df.index[-1].isoformat(),
            "severity": round(a_severity, 2),
            "baseline_value": round(a_baseline, 4) if a_baseline is not None else None,
            "actual_value": round(a_actual, 4),
        })

    clean_vals = df["value"].dropna()
    return {
        "anomalous_periods": anomalous_periods,
        "anomaly_count": len(anomalous_periods),
        "sigma_threshold_used": sigma,
        "baseline_stats": {
            "mean": round(float(clean_vals.mean()), 4),
            "std": round(float(clean_vals.std()), 4),
            "min": round(float(clean_vals.min()), 4),
            "max": round(float(clean_vals.max()), 4),
        },
    }


def _find_daily_patterns(args: dict) -> dict:
    df = _parse_history(args["data"])
    if df.empty:
        return {"error": "No data points found in the provided history."}

    df["hour"] = df["timestamp"].dt.hour
    df["day_of_week"] = df["timestamp"].dt.day_name()

    if _is_numeric(df):
        hourly = df.groupby("hour")["value"].mean()
        hourly_avgs = [round(float(hourly.get(h, 0)), 4) for h in range(24)]

        dow_mean = df.groupby("day_of_week")["value"].mean().to_dict()
        dow_variation = {k: round(float(v), 4) for k, v in dow_mean.items()}
    else:
        df["is_on"] = df["state"].str.lower().isin({"on", "true", "1", "active", "home"}).astype(float)
        hourly = df.groupby("hour")["is_on"].mean()
        hourly_avgs = [round(float(hourly.get(h, 0)), 4) for h in range(24)]

        dow_mean = df.groupby("day_of_week")["is_on"].mean().to_dict()
        dow_variation = {k: round(float(v), 4) for k, v in dow_mean.items()}

    threshold_75 = float(np.percentile([v for v in hourly_avgs if v > 0], 75)) if any(v > 0 for v in hourly_avgs) else 0
    peak_hours = [h for h, v in enumerate(hourly_avgs) if v >= threshold_75]

    max_val = max(hourly_avgs) if hourly_avgs else 0
    min_val = min(hourly_avgs) if hourly_avgs else 0
    if max_val > 0:
        peak_time = f"{peak_hours[0]:02d}:00–{peak_hours[-1]+1:02d}:00" if peak_hours else "N/A"
        dominant_pattern = f"Peak activity typically between {peak_time}."
    else:
        dominant_pattern = "No clear activity pattern detected."

    return {
        "hourly_averages": hourly_avgs,
        "peak_hours": peak_hours,
        "day_of_week_variation": dow_variation,
        "dominant_pattern": dominant_pattern,
    }


def _generate_chart(args: dict) -> dict:
    df = _parse_history(args["data"])
    chart_type = args["chart_type"]
    title = args["title"]
    threshold = args.get("threshold")

    if df.empty:
        return {"error": "No data points found in the provided history."}

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")
    ax.tick_params(colors="#cdd6f4")
    ax.xaxis.label.set_color("#cdd6f4")
    ax.yaxis.label.set_color("#cdd6f4")
    ax.title.set_color("#cdd6f4")
    for spine in ax.spines.values():
        spine.set_edgecolor("#45475a")

    try:
        if chart_type == "timeseries":
            _chart_timeseries(ax, df, threshold)
        elif chart_type == "histogram":
            _chart_histogram(ax, df)
        elif chart_type == "heatmap":
            _chart_heatmap(fig, ax, df)
        elif chart_type == "daily_pattern":
            _chart_daily_pattern(ax, df)

        ax.set_title(title, pad=10)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("ascii")
    finally:
        plt.close(fig)

    safe_title = title.lower().replace(" ", "_").replace("/", "-")[:48]
    return {
        "base64_png": b64,
        "suggested_artifact_name": f"{safe_title}_{chart_type}.png",
    }


def _chart_timeseries(ax, df: pd.DataFrame, threshold=None):
    if _is_numeric(df):
        ax.plot(df["timestamp"], df["value"], color="#89b4fa", linewidth=0.8, label="value")
        ax.set_ylabel("Value")
    else:
        numeric_state = df["state"].map(lambda s: 1 if s.lower() in {"on", "true", "1", "active", "home"} else 0)
        ax.step(df["timestamp"], numeric_state, color="#a6e3a1", linewidth=1.0, where="post", label="state")
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["off", "on"])
        ax.set_ylabel("State")

    if threshold is not None:
        ax.axhline(y=threshold, color="#f38ba8", linestyle="--", linewidth=1.0, label=f"threshold={threshold}")
        ax.legend(facecolor="#313244", labelcolor="#cdd6f4")

    ax.set_xlabel("Time")


def _chart_histogram(ax, df: pd.DataFrame):
    if _is_numeric(df):
        vals = df["value"].dropna()
        ax.hist(vals, bins=50, color="#89dceb", edgecolor="#313244")
        ax.set_xlabel("Value")
        ax.set_ylabel("Count")
    else:
        counts = df["state"].value_counts()
        ax.bar(counts.index, counts.values, color="#cba6f7", edgecolor="#313244")
        ax.set_xlabel("State")
        ax.set_ylabel("Count")
        plt.xticks(rotation=30, ha="right")


def _chart_heatmap(fig, ax, df: pd.DataFrame):
    df["hour"] = df["timestamp"].dt.hour
    df["dow"] = df["timestamp"].dt.dayofweek

    if _is_numeric(df):
        pivot = df.groupby(["dow", "hour"])["value"].mean().unstack(fill_value=0)
    else:
        df["is_on"] = df["state"].str.lower().isin({"on", "true", "1", "active", "home"}).astype(float)
        pivot = df.groupby(["dow", "hour"])["is_on"].mean().unstack(fill_value=0)

    pivot = pivot.reindex(range(7), fill_value=0).reindex(columns=range(24), fill_value=0)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    im = ax.imshow(pivot.values, aspect="auto", cmap="Blues", vmin=0)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}h" for h in range(0, 24, 2)])
    ax.set_yticks(range(7))
    ax.set_yticklabels(days)
    ax.set_xlabel("Hour of Day")
    fig.colorbar(im, ax=ax)


def _chart_daily_pattern(ax, df: pd.DataFrame):
    df["hour"] = df["timestamp"].dt.hour

    if _is_numeric(df):
        hourly = df.groupby("hour")["value"].mean()
        label = "Mean Value"
    else:
        df["is_on"] = df["state"].str.lower().isin({"on", "true", "1", "active", "home"}).astype(float)
        hourly = df.groupby("hour")["is_on"].mean()
        label = "Fraction Active"

    avgs = [float(hourly.get(h, 0)) for h in range(24)]
    ax.bar(range(24), avgs, color="#a6e3a1", edgecolor="#313244")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}h" for h in range(0, 24, 2)])
    ax.set_xlabel("Hour of Day")
    ax.set_ylabel(label)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
