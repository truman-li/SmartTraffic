from __future__ import annotations

import io
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

try:
    from module1_service import _sanitize_ssl_env
except Exception:  # pragma: no cover
    def _sanitize_ssl_env() -> None:
        return

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
MODULE4_ROOT = WORKSPACE_ROOT / "Module-4"
MODULE4_INPUT_DIR = MODULE4_ROOT / "input"
MODULE4_CACHE_DIR = MODULE4_ROOT / "cache"
MODULE4_CACHE_DOCS_DIR = MODULE4_CACHE_DIR / "docs"

LATEST_UPLOAD_PATH = MODULE4_INPUT_DIR / "latest_upload.xlsx"
EVENTS_CSV_PATH = MODULE4_CACHE_DIR / "events.csv"
META_JSON_PATH = MODULE4_CACHE_DIR / "meta.json"
DOCS_INDEX_JSON_PATH = MODULE4_CACHE_DIR / "documents.json"

ROOT_ENV_FILE = WORKSPACE_ROOT / ".env"

REQUIRED_COLUMNS = ("车牌号", "过车时间", "点位名称")
POINT_CODE_PATTERN = re.compile(r"^(F\d+)")
DEFAULT_MERGE_SECONDS = 0

MODULE5_CHAT_MODEL = "qwen3.6-plus"
MODULE5_CHAT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

MODULE5_QA_SYSTEM_PROMPT = (
    "你是交通轨迹研判助手。"
    "你必须只基于给定轨迹数据回答，不能虚构事实。"
    "当证据不足时，明确写“无法从当前轨迹数据确认”。"
)

MODULE5_QA_TEMPLATE = """
你将收到某辆车在“当前选中文档”中的轨迹结构化数据。
请结合用户问题进行分析，严格遵守以下规则：
1) 只允许引用提供的数据，不得编造车牌、时间、点位和行为；
2) 若问题超出数据范围，必须明确说明无法确认；
3) 回答结构固定为：
   - 轨迹概览
   - 问题分析
   - 证据引用（按序号列出）
4) 回答语言使用中文，简明、专业、可复核。

当前文档信息：
{doc_info}

车辆：{plate_no}
事件数：{event_count}

轨迹事件表（按发生顺序）：
{trajectory_table}

用户问题：
{question}
""".strip()


def ensure_dirs() -> None:
    MODULE4_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    MODULE4_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MODULE4_CACHE_DOCS_DIR.mkdir(parents=True, exist_ok=True)


def _require_pandas() -> Any:
    if pd is None:
        raise RuntimeError("pandas 未安装，无法处理轨迹 Excel 数据。")
    return pd


def _safe_upload_name(name: str) -> str:
    base = Path(str(name or "upload.xlsx")).name.strip() or "upload.xlsx"
    if not base.lower().endswith(".xlsx"):
        base = f"{base}.xlsx"
    sanitized = re.sub(r"[^\w._-]", "_", base)
    return sanitized or "upload.xlsx"


def _extract_point_code(point_name: str) -> str:
    text = str(point_name or "").strip()
    m = POINT_CODE_PATTERN.match(text)
    return m.group(1) if m else ""


def _normalize_dataframe(raw_df: Any) -> Any:
    _pd = _require_pandas()

    df = raw_df.copy()
    df.columns = [str(col).strip() for col in df.columns]

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Excel 缺少必要列: {', '.join(missing)}")

    clean = df[list(REQUIRED_COLUMNS)].copy()
    clean = clean.rename(columns={"车牌号": "plate_no", "过车时间": "pass_time", "点位名称": "point_name"})

    clean["plate_no"] = clean["plate_no"].astype(str).str.strip().str.upper()
    clean["point_name"] = clean["point_name"].astype(str).str.strip()
    clean["pass_time"] = _pd.to_datetime(clean["pass_time"], errors="coerce")

    clean = clean[(clean["plate_no"] != "") & (clean["point_name"] != "") & clean["pass_time"].notna()].copy()
    clean = clean.drop_duplicates(subset=["plate_no", "pass_time", "point_name"], keep="first")
    clean = clean.sort_values(["plate_no", "pass_time"], ascending=[True, True]).reset_index(drop=True)

    clean["pass_time_text"] = clean["pass_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    clean["point_code"] = clean["point_name"].map(_extract_point_code)
    return clean


def _write_meta(meta: dict[str, Any]) -> None:
    META_JSON_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_meta() -> dict[str, Any]:
    if not META_JSON_PATH.exists():
        return {}
    try:
        return json.loads(META_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_docs_index() -> list[dict[str, Any]]:
    if not DOCS_INDEX_JSON_PATH.exists():
        return []
    try:
        data = json.loads(DOCS_INDEX_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    docs: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and item.get("doc_id"):
            docs.append(item)
    return docs


def _save_docs_index(items: list[dict[str, Any]]) -> None:
    DOCS_INDEX_JSON_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _upsert_doc_index(entry: dict[str, Any]) -> None:
    docs = [x for x in _load_docs_index() if x.get("doc_id") != entry.get("doc_id")]
    docs.insert(0, entry)
    _save_docs_index(docs)


def _doc_csv_path_from_entry(entry: dict[str, Any]) -> Path:
    csv_file = str(entry.get("csv_file") or "").strip()
    if not csv_file or csv_file == "events.csv":
        return EVENTS_CSV_PATH
    return MODULE4_CACHE_DOCS_DIR / csv_file


def _legacy_doc_entry() -> dict[str, Any] | None:
    meta = _read_meta()
    if not meta:
        return None
    if not EVENTS_CSV_PATH.exists():
        return None

    return {
        "doc_id": str(meta.get("doc_id") or "legacy_latest"),
        "source_file": meta.get("source_file") or "legacy_latest.xlsx",
        "history_file": meta.get("history_file") or "latest_upload.xlsx",
        "uploaded_at": meta.get("uploaded_at") or datetime.now(timezone.utc).isoformat(),
        "raw_rows": int(meta.get("raw_rows") or 0),
        "clean_rows": int(meta.get("clean_rows") or 0),
        "unique_plates": int(meta.get("unique_plates") or 0),
        "unique_points": int(meta.get("unique_points") or 0),
        "time_min": meta.get("time_min"),
        "time_max": meta.get("time_max"),
        "csv_file": "events.csv",
    }


def list_uploaded_documents(limit: int = 200) -> dict[str, Any]:
    ensure_dirs()
    docs = _load_docs_index()
    valid_docs: list[dict[str, Any]] = []
    index_changed = False
    for item in docs:
        path = _doc_csv_path_from_entry(item)
        if path.exists():
            valid_docs.append(item)
        else:
            index_changed = True
    if index_changed:
        _save_docs_index(valid_docs)

    docs = valid_docs
    if not docs:
        legacy = _legacy_doc_entry()
        if legacy:
            docs = [legacy]

    latest_doc_id = str((_read_meta().get("doc_id") or "")).strip() or None
    safe_limit = max(1, min(int(limit), 5000))
    items = docs[:safe_limit]

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row["is_latest"] = bool(latest_doc_id and str(row.get("doc_id")) == latest_doc_id)
        normalized_items.append(row)

    return {
        "total": len(docs),
        "count": len(normalized_items),
        "latest_doc_id": latest_doc_id,
        "items": normalized_items,
    }


def _sync_active_doc(entry: dict[str, Any] | None) -> None:
    if not entry:
        if EVENTS_CSV_PATH.exists():
            EVENTS_CSV_PATH.unlink(missing_ok=True)
        if LATEST_UPLOAD_PATH.exists():
            LATEST_UPLOAD_PATH.unlink(missing_ok=True)
        if META_JSON_PATH.exists():
            META_JSON_PATH.unlink(missing_ok=True)
        return

    csv_path = _doc_csv_path_from_entry(entry)
    if csv_path.exists() and csv_path != EVENTS_CSV_PATH:
        shutil.copy2(csv_path, EVENTS_CSV_PATH)

    history_name = str(entry.get("history_file") or "").strip()
    if history_name:
        history_path = MODULE4_INPUT_DIR / history_name
        if history_path.exists() and history_path != LATEST_UPLOAD_PATH:
            shutil.copy2(history_path, LATEST_UPLOAD_PATH)

    _write_meta(
        {
            "doc_id": str(entry.get("doc_id") or ""),
            "source_file": entry.get("source_file"),
            "history_file": entry.get("history_file"),
            "uploaded_at": entry.get("uploaded_at"),
            "raw_rows": int(entry.get("raw_rows") or 0),
            "clean_rows": int(entry.get("clean_rows") or 0),
            "unique_plates": int(entry.get("unique_plates") or 0),
            "unique_points": int(entry.get("unique_points") or 0),
            "time_min": entry.get("time_min"),
            "time_max": entry.get("time_max"),
            "sample_plates": [],
            "top_plates": [],
            "top_points": [],
        }
    )


def delete_uploaded_document(doc_id: str) -> dict[str, Any]:
    ensure_dirs()

    target = str(doc_id or "").strip()
    if not target:
        raise ValueError("doc_id 不能为空。")

    docs = _load_docs_index()
    target_entry: dict[str, Any] | None = None
    remaining_docs: list[dict[str, Any]] = []
    for item in docs:
        if str(item.get("doc_id") or "").strip() == target:
            target_entry = item
        else:
            remaining_docs.append(item)

    deleted_files: list[str] = []
    latest_doc_id = str((_read_meta().get("doc_id") or "")).strip()

    if target_entry is not None:
        _save_docs_index(remaining_docs)

        csv_path = _doc_csv_path_from_entry(target_entry)
        if csv_path.exists() and csv_path != EVENTS_CSV_PATH:
            csv_path.unlink(missing_ok=True)
            deleted_files.append(str(csv_path.name))

        history_name = str(target_entry.get("history_file") or "").strip()
        if history_name:
            history_path = MODULE4_INPUT_DIR / history_name
            if history_path.exists() and history_path != LATEST_UPLOAD_PATH:
                history_path.unlink(missing_ok=True)
                deleted_files.append(str(history_path.name))

        if latest_doc_id == target:
            next_entry = remaining_docs[0] if remaining_docs else None
            _sync_active_doc(next_entry)

        return {
            "ok": True,
            "doc_id": target,
            "deleted": True,
            "deleted_files": deleted_files,
            "remaining": len(remaining_docs),
            "latest_doc_id": (remaining_docs[0].get("doc_id") if remaining_docs else None),
        }

    legacy = _legacy_doc_entry()
    if legacy and str(legacy.get("doc_id") or "").strip() == target:
        _sync_active_doc(None)
        _save_docs_index([])
        return {
            "ok": True,
            "doc_id": target,
            "deleted": True,
            "deleted_files": ["events.csv", "latest_upload.xlsx", "meta.json"],
            "remaining": 0,
            "latest_doc_id": None,
        }

    raise KeyError(f"未找到文档: {target}")


def _find_doc_entry(doc_id: str) -> dict[str, Any] | None:
    target = str(doc_id or "").strip()
    if not target:
        return None

    docs = _load_docs_index()
    for item in docs:
        if str(item.get("doc_id") or "").strip() == target:
            return item

    legacy = _legacy_doc_entry()
    if legacy and str(legacy.get("doc_id")) == target:
        return legacy
    return None


def _resolve_events_csv_path(doc_id: str | None = None) -> Path:
    ensure_dirs()
    if doc_id:
        entry = _find_doc_entry(doc_id)
        if not entry:
            raise KeyError(f"未找到文档: {doc_id}")

        csv_file = str(entry.get("csv_file") or "").strip()
        if csv_file == "events.csv":
            path = EVENTS_CSV_PATH
        else:
            path = MODULE4_CACHE_DOCS_DIR / csv_file

        if not path.exists():
            raise FileNotFoundError(f"文档缓存不存在: {path.name}")
        return path

    if EVENTS_CSV_PATH.exists():
        return EVENTS_CSV_PATH

    meta = _read_meta()
    latest_doc_id = str(meta.get("doc_id") or "").strip()
    if latest_doc_id:
        entry = _find_doc_entry(latest_doc_id)
        if entry:
            csv_file = str(entry.get("csv_file") or "").strip()
            if csv_file:
                candidate = MODULE4_CACHE_DOCS_DIR / csv_file
                if candidate.exists():
                    return candidate

    raise FileNotFoundError("尚未上传并解析轨迹数据，请先上传 .xlsx 文件。")


def _load_events_dataframe(doc_id: str | None = None) -> Any:
    _pd = _require_pandas()
    path = _resolve_events_csv_path(doc_id)

    df = _pd.read_csv(path, dtype=str)
    required = {"plate_no", "pass_time_text", "point_name"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError("轨迹缓存数据结构异常，请重新上传文件。")

    df["plate_no"] = df["plate_no"].astype(str).str.strip().str.upper()
    df["point_name"] = df["point_name"].astype(str).str.strip()
    df["pass_time"] = _pd.to_datetime(df["pass_time_text"], errors="coerce")

    df = df[(df["plate_no"] != "") & (df["point_name"] != "") & df["pass_time"].notna()].copy()
    df = df.sort_values(["plate_no", "pass_time"], ascending=[True, True]).reset_index(drop=True)
    return df


def build_dataset_from_upload(filename: str, file_bytes: bytes) -> dict[str, Any]:
    _pd = _require_pandas()
    ensure_dirs()

    if not file_bytes:
        raise ValueError("上传文件为空。")

    safe_name = _safe_upload_name(filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    doc_id = f"doc_{stamp}_{uuid.uuid4().hex[:8]}"
    history_path = MODULE4_INPUT_DIR / f"{stamp}_{safe_name}"

    history_path.write_bytes(file_bytes)
    LATEST_UPLOAD_PATH.write_bytes(file_bytes)

    raw_df = _pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    raw_rows = int(len(raw_df))

    clean = _normalize_dataframe(raw_df)
    clean_rows = int(len(clean))

    doc_csv_path = MODULE4_CACHE_DOCS_DIR / f"{doc_id}.csv"
    export_df = clean[["plate_no", "pass_time_text", "point_name", "point_code"]]
    export_df.to_csv(doc_csv_path, index=False, encoding="utf-8-sig")
    export_df.to_csv(EVENTS_CSV_PATH, index=False, encoding="utf-8-sig")

    unique_plates = sorted(clean["plate_no"].dropna().unique().tolist())
    unique_points = sorted(clean["point_name"].dropna().unique().tolist())

    top_plate_series = clean.groupby("plate_no").size().sort_values(ascending=False).head(10)
    top_point_series = clean.groupby("point_name").size().sort_values(ascending=False).head(10)

    summary = {
        "doc_id": doc_id,
        "source_file": safe_name,
        "history_file": history_path.name,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "raw_rows": raw_rows,
        "clean_rows": clean_rows,
        "unique_plates": len(unique_plates),
        "unique_points": len(unique_points),
        "time_min": clean["pass_time_text"].min() if clean_rows else None,
        "time_max": clean["pass_time_text"].max() if clean_rows else None,
        "sample_plates": unique_plates[:20],
        "top_plates": [
            {"plate_no": str(k), "count": int(v)}
            for k, v in top_plate_series.items()
        ],
        "top_points": [
            {"point_name": str(k), "count": int(v)}
            for k, v in top_point_series.items()
        ],
    }

    _write_meta(summary)
    _upsert_doc_index(
        {
            "doc_id": doc_id,
            "source_file": safe_name,
            "history_file": history_path.name,
            "uploaded_at": summary["uploaded_at"],
            "raw_rows": raw_rows,
            "clean_rows": clean_rows,
            "unique_plates": len(unique_plates),
            "unique_points": len(unique_points),
            "time_min": summary["time_min"],
            "time_max": summary["time_max"],
            "csv_file": doc_csv_path.name,
        }
    )
    return summary


def get_dataset_summary(doc_id: str | None = None) -> dict[str, Any]:
    if doc_id:
        entry = _find_doc_entry(doc_id)
        if not entry:
            raise KeyError(f"未找到文档: {doc_id}")
        return {
            "doc_id": entry.get("doc_id"),
            "source_file": entry.get("source_file"),
            "history_file": entry.get("history_file"),
            "uploaded_at": entry.get("uploaded_at"),
            "raw_rows": int(entry.get("raw_rows") or 0),
            "clean_rows": int(entry.get("clean_rows") or 0),
            "unique_plates": int(entry.get("unique_plates") or 0),
            "unique_points": int(entry.get("unique_points") or 0),
            "time_min": entry.get("time_min"),
            "time_max": entry.get("time_max"),
            "sample_plates": [],
            "top_plates": [],
            "top_points": [],
        }

    meta = _read_meta()
    if meta:
        return meta

    df = _load_events_dataframe()
    if df.empty:
        return {
            "doc_id": None,
            "source_file": None,
            "raw_rows": 0,
            "clean_rows": 0,
            "unique_plates": 0,
            "unique_points": 0,
            "time_min": None,
            "time_max": None,
            "sample_plates": [],
            "top_plates": [],
            "top_points": [],
        }

    return {
        "doc_id": None,
        "source_file": "unknown",
        "raw_rows": int(len(df)),
        "clean_rows": int(len(df)),
        "unique_plates": int(df["plate_no"].nunique()),
        "unique_points": int(df["point_name"].nunique()),
        "time_min": str(df["pass_time"].min()),
        "time_max": str(df["pass_time"].max()),
        "sample_plates": sorted(df["plate_no"].unique().tolist())[:20],
        "top_plates": [],
        "top_points": [],
    }


def list_unique_plates(keyword: str = "", limit: int = 5000, doc_id: str | None = None) -> dict[str, Any]:
    df = _load_events_dataframe(doc_id=doc_id)

    all_plates = sorted(df["plate_no"].dropna().unique().tolist())
    kw = str(keyword or "").strip().upper()
    filtered = [p for p in all_plates if kw in p] if kw else all_plates

    safe_limit = max(1, min(int(limit), 20000))
    items = filtered[:safe_limit]
    return {
        "doc_id": doc_id,
        "keyword": kw,
        "total": len(filtered),
        "count": len(items),
        "items": items,
    }


def _merge_consecutive_events(rows: list[dict[str, Any]], merge_seconds: int) -> list[dict[str, Any]]:
    merge_threshold = max(0, int(merge_seconds))

    if merge_threshold <= 0:
        no_merge: list[dict[str, Any]] = []
        for row in rows:
            point_name = str(row.get("point_name") or "").strip()
            pass_time = row.get("pass_time")
            if not point_name or pass_time is None:
                continue
            ts = pass_time.to_pydatetime() if hasattr(pass_time, "to_pydatetime") else pass_time
            ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")
            no_merge.append(
                {
                    "point_name": point_name,
                    "start_time": ts_text,
                    "end_time": ts_text,
                    "record_count": 1,
                }
            )
        return no_merge

    merged: list[dict[str, Any]] = []

    for row in rows:
        point_name = str(row.get("point_name") or "").strip()
        pass_time = row.get("pass_time")
        if not point_name or pass_time is None:
            continue

        ts = pass_time.to_pydatetime() if hasattr(pass_time, "to_pydatetime") else pass_time
        ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")

        if merged:
            prev = merged[-1]
            prev_end = datetime.strptime(prev["end_time"], "%Y-%m-%d %H:%M:%S")
            if prev["point_name"] == point_name:
                delta = (ts - prev_end).total_seconds()
                if delta <= merge_threshold:
                    prev["end_time"] = ts_text
                    prev["record_count"] += 1
                    continue

        merged.append(
            {
                "point_name": point_name,
                "start_time": ts_text,
                "end_time": ts_text,
                "record_count": 1,
            }
        )

    return merged


def build_plate_trajectory(
    plate_no: str,
    merge_seconds: int = DEFAULT_MERGE_SECONDS,
    doc_id: str | None = None,
) -> dict[str, Any]:
    plate = str(plate_no or "").strip().upper()
    if not plate:
        raise ValueError("车牌号不能为空。")

    df = _load_events_dataframe(doc_id=doc_id)
    sub = df[df["plate_no"] == plate].copy().sort_values("pass_time", ascending=True)
    if sub.empty:
        raise KeyError(f"未找到车牌 {plate} 的轨迹数据。")

    rows = sub[["pass_time", "point_name"]].to_dict(orient="records")
    merged_rows = _merge_consecutive_events(rows, merge_seconds=merge_seconds)

    timeline_asc: list[dict[str, Any]] = []
    for idx, row in enumerate(merged_rows, 1):
        timeline_asc.append(
            {
                "seq": idx,
                "plate_no": plate,
                "pass_time": row["start_time"],
                "end_time": row["end_time"],
                "point_name": row["point_name"],
                "point_code": _extract_point_code(row["point_name"]),
                "record_count": int(row["record_count"]),
            }
        )

    timeline_desc = list(reversed(timeline_asc))

    graph_nodes: list[dict[str, Any]] = []
    for event in timeline_asc:
        graph_nodes.append(
            {
                "id": f"N{event['seq']}",
                "seq": int(event["seq"]),
                "point_name": event["point_name"],
                "point_code": event["point_code"],
                "pass_time": event["pass_time"],
                "end_time": event["end_time"],
                "record_count": int(event["record_count"]),
            }
        )

    graph_edges: list[dict[str, Any]] = []
    for i in range(len(graph_nodes) - 1):
        cur = graph_nodes[i]
        nxt = graph_nodes[i + 1]

        cur_dt = datetime.strptime(cur["pass_time"], "%Y-%m-%d %H:%M:%S")
        nxt_dt = datetime.strptime(nxt["pass_time"], "%Y-%m-%d %H:%M:%S")
        gap_min = max(0.0, (nxt_dt - cur_dt).total_seconds() / 60.0)

        graph_edges.append(
            {
                "id": f"E{i + 1}",
                "source": cur["id"],
                "target": nxt["id"],
                "seq_from": int(cur["seq"]),
                "seq_to": int(nxt["seq"]),
                "gap_minutes": round(gap_min, 2),
            }
        )

    return {
        "doc_id": doc_id,
        "plate_no": plate,
        "event_count": len(timeline_asc),
        "node_count": len(graph_nodes),
        "edge_count": len(graph_edges),
        "timeline_asc": timeline_asc,
        "timeline_desc": timeline_desc,
        "graph": {
            "nodes": graph_nodes,
            "edges": graph_edges,
        },
    }


def _extract_completion_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content or "").strip()

    chunks: list[str] = []
    for part in content:
        if isinstance(part, dict):
            txt = part.get("text")
            if isinstance(txt, str) and txt.strip():
                chunks.append(txt.strip())
    return "\n".join(chunks).strip()


def _read_env_file_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        value = v.strip().strip('"').strip("'")
        return value or None
    return None


def _get_api_key() -> str | None:
    for name in ("DASHSCOPE_API_KEY", "API_KEY", "BAILIAN_API_KEY", "GRAPHRAG_API_KEY", "OPENROUTER_API_KEY"):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value

    for name in ("API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "GRAPHRAG_API_KEY"):
        value = _read_env_file_value(ROOT_ENV_FILE, name)
        if value:
            return value
    return None


def _build_trajectory_table_text(timeline_asc: list[dict[str, Any]]) -> str:
    if not timeline_asc:
        return "(无轨迹事件)"

    lines = [
        "序号 | 过车时间 | 结束时间 | 点位名称 | 点位编码 | 合并记录数",
        "--- | --- | --- | --- | --- | ---",
    ]
    for row in timeline_asc:
        lines.append(
            f"{int(row.get('seq') or 0)} | "
            f"{row.get('pass_time') or '-'} | "
            f"{row.get('end_time') or '-'} | "
            f"{row.get('point_name') or '-'} | "
            f"{row.get('point_code') or '-'} | "
            f"{int(row.get('record_count') or 0)}"
        )
    return "\n".join(lines)


def ask_vehicle_question(
    *,
    plate_no: str,
    question: str,
    doc_id: str | None = None,
    merge_seconds: int = DEFAULT_MERGE_SECONDS,
) -> dict[str, Any]:
    query = str(question or "").strip()
    if not query:
        raise ValueError("问题不能为空。")

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("未找到百炼 API Key，请在项目根目录 .env 中配置 API_KEY。")

    trajectory = build_plate_trajectory(plate_no=plate_no, merge_seconds=merge_seconds, doc_id=doc_id)
    timeline_asc = list(trajectory.get("timeline_asc") or [])

    doc_summary = get_dataset_summary(doc_id=doc_id)
    doc_info = (
        f"文档ID: {doc_summary.get('doc_id') or '-'}; "
        f"文件: {doc_summary.get('source_file') or '-'}; "
        f"时间范围: {doc_summary.get('time_min') or '-'} ~ {doc_summary.get('time_max') or '-'}; "
        f"有效行数: {int(doc_summary.get('clean_rows') or 0)}"
    )

    table_text = _build_trajectory_table_text(timeline_asc)
    user_prompt = MODULE5_QA_TEMPLATE.format(
        doc_info=doc_info,
        plate_no=trajectory.get("plate_no") or plate_no,
        event_count=int(trajectory.get("event_count") or 0),
        trajectory_table=table_text,
        question=query,
    )

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("openai package is required.") from exc

    try:
        _sanitize_ssl_env()
        client = OpenAI(
            base_url=MODULE5_CHAT_API_BASE,
            api_key=api_key,
            timeout=120.0,
            default_headers={
                "HTTP-Referer": "http://127.0.0.1:8000",
                "X-DashScope-Sdk": "Traffic-Module5-Trajectory-QA",
            },
        )

        completion = client.chat.completions.create(
            model=MODULE5_CHAT_MODEL,
            temperature=0.1,
            max_tokens=1800,
            messages=[
                {"role": "system", "content": MODULE5_QA_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"模型调用运行环境文件缺失: {exc}") from exc

    answer = _extract_completion_text(completion.choices[0].message.content)
    if not answer:
        raise RuntimeError("模型未返回有效回答。")

    return {
        "success": True,
        "model": MODULE5_CHAT_MODEL,
        "doc_id": doc_summary.get("doc_id") or doc_id,
        "plate_no": trajectory.get("plate_no") or plate_no,
        "event_count": int(trajectory.get("event_count") or 0),
        "answer": answer,
        "prompt_template": "module5_trajectory_qa_v1",
    }
