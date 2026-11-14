"""服务事件窗口Jaccard和最近后继的有向时滞关联。"""
from collections import defaultdict
import numpy as np


def metadata():
    return {
        "name": "服务故障共现研判", "short": "incident-links",
        "tagline": "把同窗共现和先后跟随分开量化，定位值得复查的传播链。",
        "client": "桥石数服 · 7人可靠性小组 · 5个服务的告警复核事件",
        "workflow": "值班工程师翻阅多份告警时间线，手工判断缓存、网关与队列异常是否同时出现。",
        "pains": ["告警次数不同使原始共同次数不可比", "同窗共现容易被误读成先后传播", "人工时间线无法复现实验窗口变化"],
        "upstream": "已去除重复通知的事件归档 service_events_week45.csv；时间以周一00:00起累计分钟表示",
        "downstream": "可靠性复盘会对高Jaccard与短时滞的服务对核查调用日志",
        "entities": ["服务复核事件", "时窗关联试验", "可靠性复盘简报"],
        "dataset_name": "桥石五服务本周复核事件序列",
        "columns": [{"key": "event_id", "label": "复核事件ID", "type": "string"}, {"key": "service", "label": "服务名称", "type": "string"}, {"key": "occurred_min", "label": "周内累计分钟", "type": "number"}, {"key": "severity", "label": "事件等级(1~3)", "type": "number"}],
        "parameters": [{"key": "window_min", "label": "同窗及跟随窗口(分钟)", "type": "number", "default": 5, "min": 1, "max": 30}, {"key": "min_pairs", "label": "最少跟随事件数", "type": "number", "default": 3, "min": 2, "max": 20}],
        "workbench_title": "服务事件时窗复盘", "action_label": "计算共现与有向时滞", "unit": "个复核事件",
        "accent": "oklch(63% 0.12 155)", "style": "control-room-industrial",
        "seed_story": ["09:00 值班工程师导入 service_events_week45.csv", "09:10 确认事件已按event_id去重，时间原点均为周一00:00", "09:20 选择5分钟窗口、至少3次跟随", "09:21 后端分别计算占用时间窗Jaccard与最近后继时滞", "09:30 工程师比较cache→api与api→cache方向的跟随率", "09:40 将窗口改为10分钟，查看链条扩张", "10:00 下载服务对明细，回查原始调用链", "17:00 管理员安排每周一09:00可靠性复盘简报"],
        "method": "固定窗口以累计分钟0为原点；Jaccard=共同占用窗/任一服务占用窗。对每个A事件，仅匹配窗口内最早且严格晚于它的B事件；跟随率=匹配A事件数/A总事件数，输出中位时滞。每个A独立匹配，允许不同A共享B。",
        "limitations": ["窗口边界会影响Jaccard，必须比较多个窗口", "多个A可共享后继B，不表示一对一故障传播", "共现或先后关系均不足以证明因果"],
    }


def seed_records():
    rng = np.random.default_rng(1013)
    events = []
    def add(service, minute, severity):
        events.append({"event_id": f"QS-{len(events)+1:04}", "service": service, "occurred_min": round(float(minute), 2), "severity": int(severity)})
    for i in range(55):
        base = i * 60 + float(rng.uniform(4, 15))
        add("cache", base, 2)
        add("api-gateway", base + float(rng.uniform(1.5, 4.5)), 2)
        if i % 3 == 0:
            add("job-queue", base + float(rng.uniform(6, 11)), 1)
        if i % 4 == 0:
            add("metrics-db", base - float(rng.uniform(2, 4)), 3)
    for _ in range(45):
        add(str(rng.choice(["cache", "api-gateway", "job-queue", "metrics-db", "object-store"])), rng.uniform(0, 3300), rng.integers(1, 4))
    return sorted(events, key=lambda row: row["occurred_min"])


def analyze(records, params):
    try:
        window = float(params.get("window_min", 5))
        raw_min = float(params.get("min_pairs", 3))
        minimum = int(raw_min)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("窗口与最少跟随次数须为有效数值。") from exc
    if not 1 <= window <= 30 or raw_min != minimum or not 2 <= minimum <= 20:
        raise ValueError("窗口须1~30分钟，最少跟随数须2~20的整数。")
    if len(records) < 30:
        raise ValueError("至少需要30个已去重事件。")
    grouped = defaultdict(list)
    identifiers = set()
    for row in records:
        try:
            service = str(row["service"]).strip()
            minute = float(row["occurred_min"])
            severity = float(row["severity"])
            event_id = str(row["event_id"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("事件ID、服务、累计分钟与等级须完整。") from exc
        if not service or not event_id or not np.isfinite(minute) or minute < 0 or severity not in (1, 2, 3):
            raise ValueError("事件ID与服务不能为空，时间须非负有限，等级须1~3。")
        if event_id in identifiers:
            raise ValueError("事件ID重复，请先去重，避免人为放大关联。")
        identifiers.add(event_id)
        grouped[service].append(minute)
    if not 2 <= len(grouped) <= 12:
        raise ValueError("一次试验须包含2~12个服务。")
    times = {key: np.sort(values) for key, values in grouped.items()}
    occupied = {key: set(np.floor(values / window).astype(int).tolist()) for key, values in times.items()}
    rows = []
    for a in sorted(times):
        for b in sorted(times):
            if a == b:
                continue
            lags = []
            for instant in times[a]:
                index = int(np.searchsorted(times[b], instant, side="right"))
                if index < len(times[b]) and times[b][index] - instant <= window:
                    lags.append(float(times[b][index] - instant))
            if len(lags) < minimum:
                continue
            union = len(occupied[a] | occupied[b])
            intersection = len(occupied[a] & occupied[b])
            rows.append({"source": a, "target": b, "source_events": len(times[a]), "follow_count": len(lags), "follow_pct": round(len(lags) / len(times[a]) * 100, 2), "median_lag_min": round(float(np.median(lags)), 3), "jaccard": round(intersection / union, 4), "shared_windows": intersection})
    rows.sort(key=lambda row: (-row["follow_pct"], -row["follow_count"], row["source"], row["target"]))
    for rank, row in enumerate(rows, 1):
        row["link_id"] = f"L{rank:02}"
    top = rows[:8]
    return {
        "summary": f"{len(records)}个事件覆盖{len(times)}个服务；{window:g}分钟窗口下，{len(rows)}条有向服务对满足至少{minimum}次跟随。",
        "metrics": [{"label": "复核事件", "value": len(records)}, {"label": "服务数量", "value": len(times)}, {"label": "有向关联候选", "value": len(rows)}, {"label": "最高跟随率", "value": max([row["follow_pct"] for row in rows], default=0), "unit": "%"}],
        "table": {"columns": [{"key": "link_id", "label": "服务对编号"}, {"key": "source", "label": "先发生服务A"}, {"key": "target", "label": "后继服务B"}, {"key": "source_events", "label": "A事件数"}, {"key": "follow_count", "label": "跟随次数"}, {"key": "follow_pct", "label": "跟随率(%)"}, {"key": "median_lag_min", "label": "中位时滞(min)"}, {"key": "jaccard", "label": "同窗Jaccard"}, {"key": "shared_windows", "label": "共同占用窗"}], "rows": rows},
        "chart": {"type": "bar", "labels": [row["link_id"] for row in top], "series": ([{"name": "跟随率%（服务对编号见明细）", "values": [row["follow_pct"] for row in top]}] if top else [])},
        "findings": ([f"最强跟随方向为{rows[0]['source']}→{rows[0]['target']}，中位时滞{rows[0]['median_lag_min']}分钟。"] if rows else ["当前窗口与最少次数下没有候选服务对，可核对时间范围后调整窗口。"])+["Jaccard衡量同窗重合，有向跟随率衡量先后顺序，两者不可互相替代。", "每个源事件仅取最近后继；扩大窗口可能提高跟随率，也会引入偶然匹配。"],
        "method": metadata()["method"], "warnings": ["有向先后关系不是故障因果链，需回查调用与变更记录。"] + (["没有满足阈值的服务对。"] if not rows else []),
    }
