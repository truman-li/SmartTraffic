"""WebUI Module-1 Service — 车辆管理、嵌入检索、VLM 分析业务逻辑。

本模块从 main.py 中拆离，承担 Module-1 的全部业务逻辑：
  · SQLite 数据库初始化与访问
  · 车辆 CRUD（upsert / get / delete / list）
  · 向量嵌入：生成、缓存（SQLite）、余弦查询
  · 文本检索（LLM 语义解析 + 结构化过滤）
  · 以图搜图（向量 + 结构化双通道）
    · VLM 串行分析（直接调用 utils，无需子进程）

调用方（main.py / app 路由层）只需 import 本模块的函数，无需了解内部实现。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Callable

from fastapi import HTTPException
import httpx

# ---------------------------------------------------------------------------
# 路径常量（由 main.py 在启动时注入，这里提供合理默认值）
# ---------------------------------------------------------------------------

WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
MODULE1_ROOT: Path = WORKSPACE_ROOT / "Module-1"
MODULE1_IMGS_DIR: Path = MODULE1_ROOT / "vehicle_imgs"
MODULE1_SCRIPTS_DIR: Path = MODULE1_ROOT / "scripts"
MODULE1_VEHICLES_DIR: Path = MODULE1_ROOT / "vehicles"
MODULE1_DB_FILE: Path = MODULE1_ROOT / "module1.sqlite3"
MODULE1_CHAT_MODEL = "qwen3.6-plus"
MODULE1_EMBEDDING_MODEL = "multimodal-embedding-v1"
MODULE1_CHAT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODULE1_MULTIMODAL_EMBED_API = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
# <=0 表示不限制单次检索补建数量（全量补建缺失向量）
MODULE1_MAX_EMBED_REBUILD_PER_SEARCH = 0
MODULE1_TEXT_PLAN_USE_LLM = False
MODULE1_TEXT_PLAN_TIMEOUT_SECONDS = 6.0
MODULE1_TEXT_EMBED_TIMEOUT_SECONDS = 6.0
MODULE1_AGENT_HISTORY_LIMIT = 8
MODULE1_AGENT_MAX_TOOL_ROUNDS = 4
MODULE1_AGENT_TOOL_TOPK_MAX = 30
MODULE1_CONFIG_FILE: Path = MODULE1_ROOT / "config.yaml"
ROOT_ENV_FILE: Path = WORKSPACE_ROOT / ".env"

MODULE1_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VEHICLE_IMAGE_PATTERN = re.compile(r"^vehicle_(\d+)\.(jpg|jpeg|png|bmp|webp)$", re.IGNORECASE)
VEHICLE_JSON_PATTERN = re.compile(r"^vehicle_(\d+)\.json$", re.IGNORECASE)

# 确保 Module-1/scripts 在 sys.path 中，以便 import utils
if str(MODULE1_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE1_SCRIPTS_DIR))

from utils import (  # noqa: E402
    analyze_with_openrouter,
    extract_first_json_object,
    guess_mime_type,
    normalize_embedding,
    normalize_result,
    normalize_vehicle_type,
    parse_vehicle_id,
    vector_cosine_similarity,
)

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

_name_lock = threading.Lock()
_openrouter_client: Any | None = None

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path) -> str:
    return str(path.relative_to(WORKSPACE_ROOT)).replace("\\", "/")


def _sanitize_ssl_env() -> None:
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        val = os.getenv(var)
        if val and not Path(val).exists():
            os.environ.pop(var, None)


def _read_simple_yaml_scalar_map(path: Path) -> dict[str, str]:
    """读取简单 `key: value` 形式的 YAML 映射（仅顶层标量）。"""
    pairs: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ_")):
                # 仅支持顶层环境变量风格键，跳过缩进块与其它复杂结构。
                continue
            if ":" not in line:
                continue
            key, raw_val = line.split(":", 1)
            key = key.strip()
            if not key:
                continue

            value = raw_val.strip()
            if value and not (value.startswith('"') or value.startswith("'")):
                idx = value.find(" #")
                if idx >= 0:
                    value = value[:idx].rstrip()
                if value.startswith("#"):
                    value = ""

            parsed = value.strip().strip('"').strip("'")
            if parsed:
                pairs[key] = parsed
    except Exception:
        return {}
    return pairs


def _read_module1_config_value(name: str) -> str | None:
    if not MODULE1_CONFIG_FILE.exists():
        return None
    pairs = _read_simple_yaml_scalar_map(MODULE1_CONFIG_FILE)
    val = pairs.get(name)
    if val and str(val).strip():
        return str(val).strip()
    return None


def _read_root_env_value(name: str) -> str | None:
    if not ROOT_ENV_FILE.exists():
        return None
    try:
        for raw_line in ROOT_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_val = line.split("=", 1)
            if key.strip() != name:
                continue
            parsed = raw_val.strip().strip('"').strip("'")
            if parsed:
                return parsed
    except Exception:
        return None
    return None


def _read_env_or_dotenv(name: str) -> str | None:
    value = os.getenv(name)
    if value and str(value).strip():
        return str(value).strip()

    cfg_val = _read_module1_config_value(name)
    if cfg_val is not None:
        return cfg_val

    return _read_root_env_value(name)


def _parse_bool_option(name: str, default: bool) -> bool:
    raw = _read_env_or_dotenv(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_float_option(name: str, default: float, lower: float, upper: float) -> float:
    raw = _read_env_or_dotenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def _parse_int_option(name: str, default: int, lower: int, upper: int) -> int:
    raw = _read_env_or_dotenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def _module1_enable_thinking() -> bool:
    return _parse_bool_option("MODULE1_ENABLE_THINKING", False)


def _chat_completion_create(client: Any, **kwargs: Any) -> Any:
    req = dict(kwargs)
    req["extra_body"] = {"enable_thinking": _module1_enable_thinking()}
    try:
        return client.chat.completions.create(**req)
    except Exception as exc:
        err = str(exc).lower()
        if (
            "enable_thinking" in err
            or "extra_body" in err
            or "unexpected keyword argument" in err
        ) and "extra_body" in req:
            req.pop("extra_body", None)
            return client.chat.completions.create(**req)
        raise


def _get_module1_vlm_runtime_config() -> dict[str, Any]:
    cfg = {
        "enable_thinking": _parse_bool_option("MODULE1_VLM_ENABLE_THINKING", _module1_enable_thinking()),
        "temperature": _parse_float_option("MODULE1_VLM_TEMPERATURE", 0.0, 0.0, 1.0),
        "max_tokens_fast": _parse_int_option("MODULE1_VLM_MAX_TOKENS_FAST", 2048, 64, 4096),
        "max_tokens_retry": _parse_int_option("MODULE1_VLM_MAX_TOKENS_RETRY", 2048, 64, 4096),
    }
    if int(cfg["max_tokens_retry"]) < int(cfg["max_tokens_fast"]):
        cfg["max_tokens_retry"] = int(cfg["max_tokens_fast"])
    return cfg


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def get_openrouter_api_key() -> str | None:
    for name in ("DASHSCOPE_API_KEY", "API_KEY", "BAILIAN_API_KEY", "GRAPHRAG_API_KEY", "OPENROUTER_API_KEY"):
        value = _read_env_or_dotenv(name)
        if value:
            return value
    return None


def get_openrouter_client() -> Any:
    global _openrouter_client
    if _openrouter_client is not None:
        return _openrouter_client
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai package is required.") from exc
    api_key = get_openrouter_api_key()
    if not api_key:
        raise RuntimeError(
            "Bailian API key not found. Set API_KEY in project-root .env (or env DASHSCOPE_API_KEY/BAILIAN_API_KEY/GRAPHRAG_API_KEY)."
        )
    _sanitize_ssl_env()
    _openrouter_client = OpenAI(
        base_url=MODULE1_CHAT_API_BASE,
        api_key=api_key,
        timeout=45,
        default_headers={
            "HTTP-Referer": "http://127.0.0.1:8000",
            "X-DashScope-Sdk": "Traffic-Module1-Structured-Retrieve",
        },
    )
    return _openrouter_client


# ---------------------------------------------------------------------------
# 文件名与路径工具
# ---------------------------------------------------------------------------

def parse_vehicle_id_from_image_name(image_name: str) -> int | None:
    return parse_vehicle_id(image_name)


def _scan_vehicle_indices() -> set[int]:
    indices: set[int] = set()
    if not MODULE1_IMGS_DIR.exists():
        return indices
    for entry in MODULE1_IMGS_DIR.iterdir():
        if not entry.is_file():
            continue
        m = VEHICLE_IMAGE_PATTERN.match(entry.name)
        if m:
            indices.add(int(m.group(1)))
    return indices


def allocate_vehicle_image_path(used: set[int], suffix: str) -> Path:
    n = 1
    while n in used:
        n += 1
    used.add(n)
    return MODULE1_IMGS_DIR / f"vehicle_{n}{suffix.lower()}"


def _scan_vehicle_images() -> list[Path]:
    images: list[tuple[int, Path]] = []
    if not MODULE1_IMGS_DIR.exists():
        return []
    for entry in MODULE1_IMGS_DIR.iterdir():
        if not entry.is_file():
            continue
        m = VEHICLE_IMAGE_PATTERN.match(entry.name)
        if m:
            images.append((int(m.group(1)), entry))
    images.sort(key=lambda x: x[0])
    return [p for _, p in images]


def resolve_vehicle_paths_from_names(image_names: list[str]) -> list[Path]:
    rows: list[tuple[int, Path]] = []
    for name in image_names:
        if not name or "/" in name or "\\" in name:
            continue
        m = VEHICLE_IMAGE_PATTERN.match(name)
        if m is None:
            continue
        path = MODULE1_IMGS_DIR / name
        if path.exists() and path.is_file():
            rows.append((int(m.group(1)), path))
    rows.sort(key=lambda x: x[0])
    return [p for _, p in rows]


def resolve_vehicle_image_name(vehicle_id: int, preferred_name: str | None = None) -> str | None:
    preferred = str(preferred_name or "").strip()
    preferred_vid = parse_vehicle_id(preferred) if preferred else None
    if preferred and preferred_vid == vehicle_id:
        if (MODULE1_IMGS_DIR / preferred).exists():
            return preferred
    for suffix in sorted(MODULE1_IMAGE_SUFFIXES):
        candidate = f"vehicle_{vehicle_id}{suffix}"
        if (MODULE1_IMGS_DIR / candidate).exists():
            return candidate
    return preferred if (preferred and preferred_vid == vehicle_id) else None


def resolve_vehicle_image_path(vehicle_id: int, preferred_name: str | None = None) -> Path | None:
    name = resolve_vehicle_image_name(vehicle_id, preferred_name)
    if not name:
        return None
    path = MODULE1_IMGS_DIR / name
    return path if path.exists() and path.is_file() else None


def guess_image_mime_type(image_name: str) -> str:
    return guess_mime_type(image_name)


# ---------------------------------------------------------------------------
# SQLite 数据库
# ---------------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    MODULE1_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MODULE1_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _vehicle_table_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(vehicles)").fetchall()
    return [str(r["name"]) for r in rows]


def _rebuild_vehicle_table(conn: sqlite3.Connection, source_cols: set[str]) -> None:
    # SQLite 表重建迁移：统一到当前固定字段结构。
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS vehicles__new")
    conn.execute("""
        CREATE TABLE vehicles__new (
            vehicle_id INTEGER PRIMARY KEY,
            image_name TEXT NOT NULL UNIQUE,
            image_path TEXT,
            type TEXT,
            type_info TEXT,
            brand TEXT,
            color TEXT,
            material TEXT,
            sign TEXT,
            structure TEXT,
            exception TEXT,
            has_plate INTEGER,
            plate TEXT,
            upload_date TEXT,
            other_info TEXT,
            source_json_file TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    def col_or_null(name: str) -> str:
        return name if name in source_cols else "NULL"

    color_expr = col_or_null("color")
    material_expr = col_or_null("material")
    updated_at_expr = "updated_at" if "updated_at" in source_cols else "datetime('now')"

    conn.execute(
        f"""
        INSERT INTO vehicles__new (
            vehicle_id, image_name, image_path, type, type_info, brand,
            color, material, sign, structure, exception,
            has_plate, plate, upload_date, other_info, source_json_file, updated_at
        )
        SELECT
            vehicle_id,
            image_name,
            {col_or_null("image_path")},
            {col_or_null("type")},
            {col_or_null("type_info")},
            {col_or_null("brand")},
            {color_expr},
            {material_expr},
            {col_or_null("sign")},
            {col_or_null("structure")},
            {col_or_null("exception")},
            {col_or_null("has_plate")},
            {col_or_null("plate")},
            {col_or_null("upload_date")},
            {col_or_null("other_info")},
            {col_or_null("source_json_file")},
            {updated_at_expr}
        FROM vehicles
        """
    )
    conn.execute("DROP TABLE vehicles")
    conn.execute("ALTER TABLE vehicles__new RENAME TO vehicles")
    conn.execute("PRAGMA foreign_keys = ON")


def _ensure_vehicle_table_schema(conn: sqlite3.Connection) -> None:
    expected = [
        "vehicle_id", "image_name", "image_path", "type", "type_info", "brand",
        "color", "material", "sign", "structure", "exception",
        "has_plate", "plate", "upload_date", "other_info", "source_json_file", "updated_at",
    ]
    current = _vehicle_table_columns(conn)
    if not current:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                vehicle_id INTEGER PRIMARY KEY,
                image_name TEXT NOT NULL UNIQUE,
                image_path TEXT,
                type TEXT,
                type_info TEXT,
                brand TEXT,
                color TEXT,
                material TEXT,
                sign TEXT,
                structure TEXT,
                exception TEXT,
                has_plate INTEGER,
                plate TEXT,
                upload_date TEXT,
                other_info TEXT,
                source_json_file TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        return
    if current == expected:
        return
    _rebuild_vehicle_table(conn, set(current))


def init_db() -> None:
    with db_connect() as conn:
        _ensure_vehicle_table_schema(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                query_text TEXT,
                query_payload TEXT,
                top_k INTEGER NOT NULL,
                returned_count INTEGER NOT NULL,
                latency_ms REAL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("DROP INDEX IF EXISTS idx_vehicles_color")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_type ON vehicles(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_color ON vehicles(color)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_material ON vehicles(material)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_upload_date ON vehicles(upload_date)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_embeddings (
                vehicle_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (vehicle_id, model),
                FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id) ON DELETE CASCADE
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# 车辆行：序列化 / 反序列化
# ---------------------------------------------------------------------------

def _to_plate_flag(value: Any) -> int | None:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return 1
        if lowered in {"false", "0", "no", "n"}:
            return 0
    return None


def _sqlite_row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    vehicle_id = int(row["vehicle_id"])
    raw_name = str(row["image_name"] or "").strip()
    resolved_name = resolve_vehicle_image_name(vehicle_id, raw_name)
    image_name = resolved_name or raw_name or f"vehicle_{vehicle_id}.jpg"
    if resolved_name and resolved_name != raw_name:
        _repair_image_name_row(vehicle_id, resolved_name)

    has_image = resolve_vehicle_image_path(vehicle_id, image_name) is not None
    image_url = f"/api/module1/image-by-id/{vehicle_id}" if has_image else None
    plate_raw = row["plate"]
    plate = str(plate_raw).strip() if isinstance(plate_raw, str) and plate_raw.strip() else None
    has_plate_raw = row["has_plate"]
    has_plate = bool(has_plate_raw) if has_plate_raw is not None else None
    return {
        "vehicle_id": vehicle_id,
        "image_name": image_name,
        "image_url": image_url,
        "type": row["type"],
        "type_info": row["type_info"],
        "brand": row["brand"],
        "color": row["color"],
        "material": row["material"],
        "sign": row["sign"],
        "structure": row["structure"],
        "exception": row["exception"],
        "has_plate": has_plate,
        "plate": plate,
        "upload_date": row["upload_date"],
        "other_info": row["other_info"],
        "updated_at": row["updated_at"],
    }


def _repair_image_name_row(vehicle_id: int, image_name: str) -> None:
    image_path = MODULE1_IMGS_DIR / image_name
    rel = _rel(image_path) if image_path.exists() else None
    with db_connect() as conn:
        conn.execute(
            "UPDATE vehicles SET image_name=?, image_path=?, updated_at=? WHERE vehicle_id=?",
            (image_name, rel, _now_iso(), vehicle_id),
        )
        conn.commit()


def repair_vehicle_image_names() -> int:
    with db_connect() as conn:
        rows = conn.execute("SELECT vehicle_id, image_name FROM vehicles").fetchall()
        updates: list[tuple[int, str]] = []
        for row in rows:
            vid = int(row["vehicle_id"])
            current = str(row["image_name"] or "").strip()
            resolved = resolve_vehicle_image_name(vid, current)
            if resolved and resolved != current:
                updates.append((vid, resolved))
        if not updates:
            return 0
        now = _now_iso()
        for vid, name in updates:
            path = MODULE1_IMGS_DIR / name
            rel = _rel(path) if path.exists() else None
            conn.execute(
                "UPDATE vehicles SET image_name=?, image_path=?, updated_at=? WHERE vehicle_id=?",
                (name, rel, now, vid),
            )
        conn.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# 车辆 CRUD
# ---------------------------------------------------------------------------

def upsert_vehicle_row(payload: dict[str, Any], source_json_file: str | None = None) -> None:
    image_name = str(payload.get("image_name") or "").strip()
    vehicle_id = payload.get("vehicle_id")
    if not isinstance(vehicle_id, int):
        vehicle_id = parse_vehicle_id(image_name) if image_name else None
    if not isinstance(vehicle_id, int):
        return

    resolved = resolve_vehicle_image_name(vehicle_id, image_name)
    if resolved:
        image_name = resolved
    elif not image_name or parse_vehicle_id(image_name) != vehicle_id:
        image_name = f"vehicle_{vehicle_id}.jpg"

    image_path = resolve_vehicle_image_path(vehicle_id, image_name)
    image_path_rel = _rel(image_path) if image_path is not None else None
    upload_date = str(payload.get("upload_date") or datetime.now(timezone.utc).date().isoformat()).strip()

    plate = str(payload.get("plate") or "").strip() or None
    other_info = payload.get("other_info")
    other_info = (other_info.strip() or None) if isinstance(other_info, str) else None
    type_info = payload.get("type_info")
    type_info = (type_info.strip() or None) if isinstance(type_info, str) else None
    color = payload.get("color")
    color = (color.strip() or None) if isinstance(color, str) else None
    material = payload.get("material")
    material = (material.strip() or None) if isinstance(material, str) else None
    sign = payload.get("sign")
    sign = (sign.strip() or None) if isinstance(sign, str) else None
    structure = payload.get("structure")
    structure = (structure.strip() or None) if isinstance(structure, str) else None
    exception = payload.get("exception")
    exception = (exception.strip() or None) if isinstance(exception, str) else None
    has_plate = _to_plate_flag(payload.get("has_plate"))
    if has_plate is None and plate:
        has_plate = 1

    now = _now_iso()
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO vehicles (
                vehicle_id, image_name, image_path, type, type_info, brand,
                color, material, sign, structure, exception,
                has_plate, plate, upload_date, other_info,
                source_json_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_id) DO UPDATE SET
                image_name=excluded.image_name, image_path=excluded.image_path,
                type=excluded.type, type_info=excluded.type_info, brand=excluded.brand,
                color=excluded.color, material=excluded.material, sign=excluded.sign,
                structure=excluded.structure, exception=excluded.exception,
                has_plate=excluded.has_plate,
                plate=excluded.plate, upload_date=excluded.upload_date,
                other_info=excluded.other_info,
                source_json_file=excluded.source_json_file,
                updated_at=excluded.updated_at
        """, (
            vehicle_id, image_name, image_path_rel,
            str(payload.get("type") or "").strip() or None,
            type_info,
            str(payload.get("brand") or "").strip() or None,
            color, material, sign, structure, exception,
            has_plate, plate, upload_date, other_info,
            source_json_file, now,
        ))
        conn.commit()


def get_vehicle_payload(vehicle_id: int) -> dict[str, Any] | None:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
    return _sqlite_row_to_payload(row) if row is not None else None


def delete_vehicle(vehicle_id: int) -> dict[str, Any]:
    db_image_name: str | None = None
    removed_vehicle_rows = 0
    removed_embedding_rows = 0

    with db_connect() as conn:
        row = conn.execute("SELECT image_name FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        if row:
            db_image_name = str(row["image_name"] or "").strip() or None
        removed_embedding_rows = conn.execute(
            "DELETE FROM vehicle_embeddings WHERE vehicle_id=?", (vehicle_id,)
        ).rowcount
        removed_vehicle_rows = conn.execute(
            "DELETE FROM vehicles WHERE vehicle_id=?", (vehicle_id,)
        ).rowcount
        conn.commit()

    image_paths: dict[str, Path] = {}
    if db_image_name and VEHICLE_IMAGE_PATTERN.match(db_image_name):
        image_paths[db_image_name.lower()] = MODULE1_IMGS_DIR / db_image_name
    for suffix in MODULE1_IMAGE_SUFFIXES:
        name = f"vehicle_{vehicle_id}{suffix}"
        image_paths[name.lower()] = MODULE1_IMGS_DIR / name

    deleted_images: list[str] = []
    for path in image_paths.values():
        if path.exists() and path.is_file():
            path.unlink(missing_ok=True)
            deleted_images.append(path.name)

    vehicle_json = MODULE1_VEHICLES_DIR / f"vehicle_{vehicle_id}.json"
    deleted_vehicle_json = False
    if vehicle_json.exists():
        vehicle_json.unlink(missing_ok=True)
        deleted_vehicle_json = True

    found = bool(removed_vehicle_rows or removed_embedding_rows or deleted_images or deleted_vehicle_json or db_image_name)
    return {
        "found": found,
        "vehicle_id": vehicle_id,
        "deleted_vehicle_row": bool(removed_vehicle_rows),
        "deleted_embedding_rows": int(removed_embedding_rows),
        "deleted_images": sorted(set(deleted_images)),
        "deleted_vehicle_json": deleted_vehicle_json,
    }


def list_vehicle_payloads(
    limit: int | None = None,
    *,
    page: int | None = None,
    page_size: int | None = None,
    analyzed_only: bool = False,
    type_filter: str | None = None,
    color_filter: str | None = None,
    material_filter: str | None = None,
    has_plate: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []
    params: list[Any] = []

    if analyzed_only:
        clauses.append("source_json_file IS NOT NULL")
    if isinstance(type_filter, str) and type_filter.strip():
        clauses.append("type=?")
        params.append(type_filter.strip())
    if isinstance(color_filter, str) and color_filter.strip():
        clauses.append("color=?")
        params.append(color_filter.strip())
    if isinstance(material_filter, str) and material_filter.strip():
        clauses.append("material=?")
        params.append(material_filter.strip())
    if isinstance(has_plate, bool):
        clauses.append("has_plate=?")
        params.append(1 if has_plate else 0)
    if isinstance(date_from, str) and date_from.strip():
        clauses.append("upload_date>=?")
        params.append(date_from.strip())
    if isinstance(date_to, str) and date_to.strip():
        clauses.append("upload_date<=?")
        params.append(date_to.strip())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(1) FROM vehicles {where}", tuple(params)).fetchone()[0])
        sql = f"SELECT * FROM vehicles {where} ORDER BY vehicle_id ASC"
        qp = list(params)
        if isinstance(page, int) and isinstance(page_size, int) and page > 0 and page_size > 0:
            sql += " LIMIT ? OFFSET ?"
            qp.extend([page_size, (page - 1) * page_size])
        elif isinstance(limit, int) and limit > 0:
            sql += " LIMIT ?"
            qp.append(limit)
        records = conn.execute(sql, tuple(qp)).fetchall()
    return [_sqlite_row_to_payload(r) for r in records], total


def persist_vehicle_json_files(results: list[dict[str, Any]]) -> list[Path]:
    MODULE1_VEHICLES_DIR.mkdir(parents=True, exist_ok=True)
    output_files: list[Path] = []
    for row in results:
        row = dict(row)
        vehicle_id = row.get("vehicle_id")
        image_name = str(row.get("image_name") or "").strip()
        parsed_id = parse_vehicle_id(image_name)
        if isinstance(parsed_id, int):
            vehicle_id = parsed_id
        elif not isinstance(vehicle_id, int):
            try:
                vehicle_id = int(vehicle_id)
            except Exception:
                continue
        row["vehicle_id"] = vehicle_id
        file_path = MODULE1_VEHICLES_DIR / f"vehicle_{vehicle_id}.json"
        file_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        output_files.append(file_path)
        upsert_vehicle_row(row, source_json_file=_rel(file_path))
    return output_files


def log_search(mode: str, query_text: str | None, query_payload: dict[str, Any], top_k: int, returned_count: int, latency_ms: float | None) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO search_logs (mode, query_text, query_payload, top_k, returned_count, latency_ms, created_at) VALUES (?,?,?,?,?,?,?)",
            (mode, query_text, json.dumps(query_payload, ensure_ascii=False), top_k, returned_count, latency_ms, _now_iso()),
        )
        conn.commit()


def _is_transient_network_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError)):
        return True
    return any(token in text for token in (
        "10054",
        "connection reset",
        "connection aborted",
        "forcibly closed",
        "远程主机强迫关闭了一个现有的连接",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "502",
        "503",
        "504",
    ))


# ---------------------------------------------------------------------------
# 嵌入向量（OpenRouter）
# ---------------------------------------------------------------------------

def openrouter_image_embedding(image_bytes: bytes, mime_type: str) -> list[float]:
    if not image_bytes:
        raise RuntimeError("Empty image bytes for embedding.")
    client = get_openrouter_client()
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    last_error: Exception | None = None
    retry_waits = (0.6, 1.2)
    for embedding_input in (
        [{"type": "input_image", "image_url": {"url": data_url}}],
        [{"type": "input_image", "image_url": data_url}],
        [{"image": data_url}],
        data_url,
    ):
        for attempt in range(len(retry_waits) + 1):
            try:
                resp = client.embeddings.create(model=MODULE1_EMBEDDING_MODEL, input=embedding_input)
                data = getattr(resp, "data", None)
                if not data:
                    break
                first = data[0]
                vector = getattr(first, "embedding", None)
                if vector is None and isinstance(first, dict):
                    vector = first.get("embedding")
                if isinstance(vector, list):
                    normalized = normalize_embedding(vector)
                    if normalized:
                        return normalized
                break
            except Exception as exc:
                last_error = exc
                if attempt < len(retry_waits) and _is_transient_network_error(exc):
                    sleep(retry_waits[attempt])
                    continue
                break

    # 回退到百炼原生多模态 Embedding 接口（兼容模型 multimodal-embedding-v1）
    api_key = get_openrouter_api_key()
    if api_key:
        for attempt in range(len(retry_waits) + 1):
            try:
                with httpx.Client(timeout=45.0, http2=False) as http:
                    r = http.post(
                        MODULE1_MULTIMODAL_EMBED_API,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": MODULE1_EMBEDDING_MODEL,
                            "input": {
                                "contents": [
                                    {"image": data_url}
                                ]
                            },
                        },
                    )
                    r.raise_for_status()
                    payload = r.json()

                # 常见返回：output.embeddings[0].embedding / output.embeddings.embedding
                output = payload.get("output") if isinstance(payload, dict) else None
                embeddings_obj = output.get("embeddings") if isinstance(output, dict) else None
                if isinstance(embeddings_obj, list) and embeddings_obj:
                    emb = embeddings_obj[0].get("embedding") if isinstance(embeddings_obj[0], dict) else None
                    if isinstance(emb, list):
                        normalized = normalize_embedding(emb)
                        if normalized:
                            return normalized
                if isinstance(embeddings_obj, dict):
                    emb = embeddings_obj.get("embedding")
                    if isinstance(emb, list):
                        normalized = normalize_embedding(emb)
                        if normalized:
                            return normalized
                break
            except Exception as exc:
                last_error = exc
                if attempt < len(retry_waits) and _is_transient_network_error(exc):
                    sleep(retry_waits[attempt])
                    continue
                break

    if last_error is not None:
        raise RuntimeError(f"Bailian image embedding failed: {last_error}") from last_error
    raise RuntimeError("Bailian image embedding: empty output.")


def get_cached_embedding(vehicle_id: int, model: str = MODULE1_EMBEDDING_MODEL) -> list[float] | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT embedding_json FROM vehicle_embeddings WHERE vehicle_id=? AND model=?",
            (vehicle_id, model),
        ).fetchone()
    if row is None:
        return None
    try:
        loaded = json.loads(str(row["embedding_json"] or "[]"))
    except Exception:
        return None
    if not isinstance(loaded, list):
        return None
    return normalize_embedding(loaded) or None


def save_embedding(vehicle_id: int, vector: list[float], model: str = MODULE1_EMBEDDING_MODEL) -> None:
    if not vector:
        return
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO vehicle_embeddings (vehicle_id, model, embedding_json, embedding_dim, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_id, model) DO UPDATE SET
                embedding_json=excluded.embedding_json,
                embedding_dim=excluded.embedding_dim,
                updated_at=excluded.updated_at
        """, (vehicle_id, model, json.dumps(vector, ensure_ascii=False), len(vector), _now_iso()))
        conn.commit()


def build_vehicle_image_embedding(row: dict[str, Any]) -> list[float] | None:
    vehicle_id = row.get("vehicle_id")
    if not isinstance(vehicle_id, int):
        return None
    cached = get_cached_embedding(vehicle_id)
    if cached:
        return cached
    image_path = resolve_vehicle_image_path(vehicle_id, str(row.get("image_name") or ""))
    if image_path is None:
        return None
    vector = openrouter_image_embedding(image_path.read_bytes(), guess_mime_type(image_path.name))
    save_embedding(vehicle_id, vector)
    return vector


def warmup_vehicle_embeddings(force: bool = False, limit: int | None = None) -> dict[str, Any]:
    started = perf_counter()
    rows, total = list_vehicle_payloads()
    to_process = rows[:limit] if isinstance(limit, int) and limit > 0 else rows

    success = 0
    skipped = 0
    failed = 0
    failed_items: list[dict[str, Any]] = []

    for row in to_process:
        vid = row.get("vehicle_id")
        if not isinstance(vid, int):
            skipped += 1
            continue
        if not force and get_cached_embedding(vid):
            skipped += 1
            continue
        try:
            vec = build_vehicle_image_embedding(row)
            if vec:
                success += 1
            else:
                failed += 1
                failed_items.append({"vehicle_id": vid, "reason": "embedding_empty_or_image_missing"})
        except Exception as exc:
            failed += 1
            failed_items.append({"vehicle_id": vid, "reason": str(exc)})

    return {
        "total_candidates": total,
        "processed": len(to_process),
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "failed_items": failed_items[:20],
        "force": bool(force),
        "limit": limit,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
    }


# ---------------------------------------------------------------------------
# 辅助：文本匹配
# ---------------------------------------------------------------------------

def _normalize_match_text(text: str) -> str:
    value = str(text or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。,.!！?？:：;；、\-_/\\|\[\](){}]", "", value)
    return value


def _normalize_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t for item in value if (t := str(item or "").strip())]


def _heuristic_plate_intent(query: str) -> bool | None:
    q = query.strip().lower()
    if not q:
        return None
    if any(t in q for t in ["无车牌", "没车牌", "没有车牌", "不带车牌", "无牌", "车牌没有", "车牌缺失"]):
        return False
    if any(t in q for t in ["有车牌", "带车牌", "车牌清晰", "有牌", "看得到车牌", "车牌是"]):
        return True
    return None


def _infer_from_query_tokens(
    query: str,
    candidates: list[str],
    alias_map: dict[str, list[str]] | None = None,
) -> list[str]:
    q = _normalize_match_text(query)
    if not q:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        c = str(cand or "").strip()
        if not c or c in seen:
            continue
        c_norm = _normalize_match_text(c)
        aliases = [c] + (alias_map.get(c, []) if alias_map else [])
        hit = c_norm and c_norm in q
        if not hit:
            for alias in aliases:
                if _normalize_match_text(alias) and _normalize_match_text(alias) in q:
                    hit = True
                    break
        if hit:
            seen.add(c)
            out.append(c)
    return out


def _match_any_exact(field_value: Any, options: list[str]) -> bool:
    if not options:
        return True
    text = str(field_value or "").strip().lower()
    return bool(text) and any(text == opt.strip().lower() for opt in options)


# ---------------------------------------------------------------------------
# 文本检索（LLM 语义解析）
# ---------------------------------------------------------------------------

_COLOR_ALIAS: dict[str, list[str]] = {
    "蓝色": ["蓝", "深蓝", "浅蓝"], "黑色": ["黑", "深色"],
    "白色": ["白", "浅色"], "黄色": ["黄", "金色"],
    "红色": ["红", "酒红"], "绿色": ["绿", "墨绿"],
    "银色": ["银", "灰银"], "灰色": ["灰", "深灰", "浅灰"],
}

_COLOR_FAMILY_MAP: dict[str, list[str]] = {
    "蓝": ["蓝", "浅蓝", "深蓝", "天蓝", "宝蓝", "藏蓝", "靛蓝"],
    "黑": ["黑", "深黑", "曜黑", "炭黑"],
    "白": ["白", "米白", "珍珠白", "象牙白"],
    "灰": ["灰", "银灰", "浅灰", "深灰"],
    "银": ["银", "银色", "银灰"],
    "红": ["红", "酒红", "绯红", "玫红"],
    "绿": ["绿", "墨绿", "浅绿", "深绿"],
    "黄": ["黄", "金", "金黄", "土黄"],
    "橙": ["橙", "橘", "橘黄"],
    "紫": ["紫", "深紫", "浅紫"],
    "棕": ["棕", "褐", "咖啡"],
}


def _extract_plate_like_token(query: str) -> str | None:
    text = str(query or "").strip().upper()
    if not text:
        return None
    m = re.search(r"([A-Z\u4e00-\u9fa5]{1,2}[A-Z0-9X]{4,8})", text)
    if not m:
        return None
    token = m.group(1)
    # 过于泛化的占位符不作为硬过滤
    if set(token) <= {"X"}:
        return None
    return token


def _normalize_detail_keywords(value: Any, limit: int = 6) -> list[str]:
    rows = _normalize_text_list(value)
    out: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        kw = str(raw or "").strip()
        if not kw:
            continue
        if kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
        if len(out) >= limit:
            break
    return out


def _infer_color_family(text: str) -> str | None:
    v = _normalize_match_text(text)
    if not v:
        return None
    for fam, aliases in _COLOR_FAMILY_MAP.items():
        for alias in aliases:
            alias_norm = _normalize_match_text(alias)
            if alias_norm and (alias_norm in v or v in alias_norm):
                return fam
    return None


def _infer_colors_from_query(query: str, candidates: list[str]) -> list[str]:
    q = _normalize_match_text(query)
    if not q:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        c = str(cand or "").strip()
        if not c or c in seen:
            continue
        c_norm = _normalize_match_text(c)
        if c_norm and c_norm in q:
            seen.add(c)
            out.append(c)
            continue
        fam = _infer_color_family(c)
        if not fam:
            continue
        fam_tokens = [fam] + _COLOR_FAMILY_MAP.get(fam, [])
        for token in fam_tokens:
            t_norm = _normalize_match_text(token)
            if t_norm and t_norm in q:
                seen.add(c)
                out.append(c)
                break
    return out


def _infer_generic_keywords_from_query(query: str, limit: int = 5) -> list[str]:
    text = str(query or "").strip()
    if not text:
        return []
    parts = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", text)
    stop = {
        "我要", "我想", "想要", "想找", "找", "找到", "一辆", "车辆", "车", "请", "帮我", "给我", "看看", "查询",
    }
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        token = str(p or "").strip()
        if not token or token in stop:
            continue
        if len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _match_any_color_family(field_value: Any, options: list[str]) -> bool:
    if not options:
        return True
    actual = str(field_value or "").strip()
    if not actual:
        return False
    actual_norm = _normalize_match_text(actual)
    actual_fam = _infer_color_family(actual)
    for opt in options:
        o = str(opt or "").strip()
        if not o:
            continue
        o_norm = _normalize_match_text(o)
        if actual_norm and o_norm and (actual_norm == o_norm or o_norm in actual_norm or actual_norm in o_norm):
            return True
        o_fam = _infer_color_family(o)
        if actual_fam and o_fam and actual_fam == o_fam:
            return True
    return False


def _query_plan_keyword_groups(plan: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "generic": _normalize_detail_keywords(plan.get("keywords"), limit=8),
        "type_info": _normalize_detail_keywords(plan.get("type_info_keywords"), limit=6),
        "sign": _normalize_detail_keywords(plan.get("sign_keywords"), limit=6),
        "structure": _normalize_detail_keywords(plan.get("structure_keywords"), limit=6),
        "exception": _normalize_detail_keywords(plan.get("exception_keywords"), limit=6),
        "other_info": _normalize_detail_keywords(plan.get("other_info_keywords"), limit=6),
        "ornament": _normalize_detail_keywords(plan.get("ornament_keywords"), limit=6),
    }


def _has_hard_filters(plan: dict[str, Any]) -> bool:
    return bool(
        isinstance(plan.get("has_plate"), bool)
        or _normalize_text_list(plan.get("brands"))
        or str(plan.get("plate_text") or "").strip()
    )


def _row_passes_hard_filters(row: dict[str, Any], plan: dict[str, Any]) -> bool:
    has_plate = plan.get("has_plate")
    if isinstance(has_plate, bool) and bool(row.get("has_plate")) != has_plate:
        return False

    brands = _normalize_text_list(plan.get("brands"))
    if brands and not _match_any_exact(row.get("brand"), brands):
        return False

    plate_text = str(plan.get("plate_text") or "").strip()
    if plate_text:
        row_plate = _normalize_match_text(str(row.get("plate") or ""))
        q_plate = _normalize_match_text(plate_text)
        if not row_plate:
            return False
        if q_plate not in row_plate and row_plate not in q_plate:
            return False
    return True


def _row_soft_boost(row: dict[str, Any], plan: dict[str, Any], keyword_groups: dict[str, list[str]]) -> tuple[float, float]:
    boost = 0.0
    kw_score = 0.0

    colors = _normalize_text_list(plan.get("colors"))
    materials = _normalize_text_list(plan.get("materials"))
    types = _normalize_text_list(plan.get("types"))

    if colors and _match_any_color_family(row.get("color"), colors):
        boost += 1.0
    if materials and _match_any_exact(row.get("material"), materials):
        boost += 0.8
    if types and _match_any_exact(row.get("type"), types):
        boost += 0.8

    text_blob = " ".join([
        str(row.get("type") or ""), str(row.get("brand") or ""),
        str(row.get("type_info") or ""), str(row.get("color") or ""), str(row.get("material") or ""),
        str(row.get("sign") or ""), str(row.get("structure") or ""),
        str(row.get("exception") or ""), str(row.get("other_info") or ""),
        str(row.get("plate") or ""),
    ]).lower()

    weighted_groups: list[tuple[str, float]] = [
        ("generic", 1.0),
        ("type_info", 1.2),
        ("sign", 1.1),
        ("structure", 1.0),
        ("exception", 1.0),
        ("other_info", 0.8),
        ("ornament", 1.4),
    ]
    for key, weight in weighted_groups:
        for kw in keyword_groups.get(key, []):
            token = str(kw or "").strip().lower()
            if token and token in text_blob:
                kw_score += weight

    return boost, kw_score


def openrouter_text_embedding(text: str) -> list[float]:
    query = str(text or "").strip()
    if not query:
        raise RuntimeError("Empty text for embedding.")
    client = get_openrouter_client()
    fast_client = client.with_options(timeout=MODULE1_TEXT_EMBED_TIMEOUT_SECONDS)
    last_error: Exception | None = None

    try:
        resp = fast_client.embeddings.create(model=MODULE1_EMBEDDING_MODEL, input=query)
        data = getattr(resp, "data", None)
        if data:
            first = data[0]
            vector = getattr(first, "embedding", None)
            if vector is None and isinstance(first, dict):
                vector = first.get("embedding")
            if isinstance(vector, list):
                normalized = normalize_embedding(vector)
                if normalized:
                    return normalized
    except Exception as exc:
        last_error = exc

    api_key = get_openrouter_api_key()
    if api_key:
        payload_candidates = [
            {"model": MODULE1_EMBEDDING_MODEL, "input": {"contents": [{"text": query}]}}
        ]
        for req_json in payload_candidates:
            try:
                with httpx.Client(timeout=MODULE1_TEXT_EMBED_TIMEOUT_SECONDS, http2=False) as http:
                    r = http.post(
                        MODULE1_MULTIMODAL_EMBED_API,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=req_json,
                    )
                    r.raise_for_status()
                    payload = r.json()
                output = payload.get("output") if isinstance(payload, dict) else None
                embeddings_obj = output.get("embeddings") if isinstance(output, dict) else None
                if isinstance(embeddings_obj, list) and embeddings_obj:
                    emb = embeddings_obj[0].get("embedding") if isinstance(embeddings_obj[0], dict) else None
                    if isinstance(emb, list):
                        normalized = normalize_embedding(emb)
                        if normalized:
                            return normalized
                if isinstance(embeddings_obj, dict):
                    emb = embeddings_obj.get("embedding")
                    if isinstance(emb, list):
                        normalized = normalize_embedding(emb)
                        if normalized:
                            return normalized
            except Exception as exc:
                last_error = exc

    if last_error is not None:
        raise RuntimeError(f"Bailian text embedding failed: {last_error}") from last_error
    raise RuntimeError("Bailian text embedding: empty output.")


def _semantic_text_to_image_candidates(query: str, rows: list[dict[str, Any]], top_k: int = 120) -> list[dict[str, Any]]:
    qvec = openrouter_text_embedding(query)
    scored: list[dict[str, Any]] = []

    for row in rows:
        vid = row.get("vehicle_id")
        if not isinstance(vid, int):
            continue
        vec = get_cached_embedding(vid)
        if not vec:
            continue
        item = dict(row)
        item["semantic_score"] = float(vector_cosine_similarity(qvec, vec))
        scored.append(item)

    scored.sort(key=lambda x: float(x.get("semantic_score") or 0), reverse=True)
    return scored[:top_k]


def _fuse_ranked_results(
    structured_rows: list[dict[str, Any]],
    semantic_rows: list[dict[str, Any]],
    *,
    rrf_k: int = 60,
    w_structured: float = 1.6,
    w_semantic: float = 1.0,
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}

    for idx, item in enumerate(structured_rows, 1):
        vid = item.get("vehicle_id")
        if not isinstance(vid, int):
            continue
        score = w_structured / float(rrf_k + idx)
        row = merged.setdefault(vid, dict(item))
        row["source_structured"] = True
        row["structured_rank"] = idx
        row["structured_score"] = float(item.get("score") or 0)
        row["fused_score"] = float(row.get("fused_score") or 0) + score

    for idx, item in enumerate(semantic_rows, 1):
        vid = item.get("vehicle_id")
        if not isinstance(vid, int):
            continue
        score = w_semantic / float(rrf_k + idx)
        row = merged.setdefault(vid, dict(item))
        row["source_semantic"] = True
        row["semantic_rank"] = idx
        row["semantic_score"] = float(item.get("semantic_score") or 0)
        row["fused_score"] = float(row.get("fused_score") or 0) + score

    results = list(merged.values())
    results.sort(
        key=lambda x: (
            float(x.get("fused_score") or 0),
            float(x.get("structured_score") or 0),
            float(x.get("semantic_score") or 0),
            str(x.get("updated_at") or ""),
            int(x.get("vehicle_id") or 0),
        ),
        reverse=True,
    )
    for i, item in enumerate(results, 1):
        item["rank"] = i
    return results


def _build_llm_query_plan(query: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    brands = sorted({str(r.get("brand") or "").strip() for r in rows if str(r.get("brand") or "").strip()})
    colors = sorted({str(r.get("color") or "").strip() for r in rows if str(r.get("color") or "").strip()})
    materials = sorted({str(r.get("material") or "").strip() for r in rows if str(r.get("material") or "").strip()})
    types = sorted({str(r.get("type") or "").strip() for r in rows if str(r.get("type") or "").strip()})

    fallback = {
        "has_plate": _heuristic_plate_intent(query),
        "brands": _infer_from_query_tokens(query, brands),
        "colors": _infer_colors_from_query(query, colors),
        "materials": _infer_from_query_tokens(query, materials),
        "types": _infer_from_query_tokens(query, types),
        "keywords": _infer_generic_keywords_from_query(query, limit=5),
        "plate_text": _extract_plate_like_token(query),
        "type_info_keywords": [],
        "sign_keywords": [],
        "structure_keywords": [],
        "exception_keywords": [],
        "other_info_keywords": [],
        "ornament_keywords": [],
    }

    prompt = {
        "task": "从用户车辆查询语句中提取结构化过滤条件。",
        "rules": [
            "只返回一个 JSON 对象，不要其他文本。",
            "has_plate 只能是 true/false/null。",
            "brands/colors/materials/types 必须从候选集合里挑选，无法确定就返回空数组。",
            "keywords 放对细节有帮助的中文短词，最多 5 个。",
            "plate_text 是车牌文本，可为空。",
            "type_info/sign/structure/exception/other_info/ornament 各自关键词数组最多 5 个。",
            "不要编造车辆 ID。",
        ],
        "candidate_values": {"brands": brands, "colors": colors, "materials": materials, "types": types},
        "schema": {
            "has_plate": "boolean|null", "brands": ["string"],
            "colors": ["string"], "materials": ["string"], "types": ["string"],
            "keywords": ["string"], "plate_text": "string|null",
            "type_info_keywords": ["string"], "sign_keywords": ["string"],
            "structure_keywords": ["string"], "exception_keywords": ["string"],
            "other_info_keywords": ["string"], "ornament_keywords": ["string"],
        },
        "user_query": query,
    }

    if not MODULE1_TEXT_PLAN_USE_LLM:
        return fallback

    try:
        client = get_openrouter_client().with_options(timeout=MODULE1_TEXT_PLAN_TIMEOUT_SECONDS)
        completion = _chat_completion_create(
            client,
            model=MODULE1_CHAT_MODEL,
            temperature=0, max_tokens=220,
            messages=[
                {"role": "system", "content": "你是车辆查询条件提取器。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        )
        content = (completion.choices[0].message.content or "").strip()
        parsed = extract_first_json_object(content)
        if not isinstance(parsed, dict):
            return fallback

        has_plate = parsed.get("has_plate")
        if not isinstance(has_plate, bool):
            has_plate = _heuristic_plate_intent(query)

        brand_set, color_set, material_set, type_set = set(brands), set(colors), set(materials), set(types)
        plan = {
            "has_plate": has_plate,
            "brands": [x for x in _normalize_text_list(parsed.get("brands")) if x in brand_set],
            "colors": [x for x in _normalize_text_list(parsed.get("colors")) if x in color_set],
            "materials": [x for x in _normalize_text_list(parsed.get("materials")) if x in material_set],
            "types": [x for x in _normalize_text_list(parsed.get("types")) if x in type_set],
            "keywords": _normalize_text_list(parsed.get("keywords"))[:5],
            "plate_text": str(parsed.get("plate_text") or "").strip() or fallback["plate_text"],
            "type_info_keywords": _normalize_detail_keywords(parsed.get("type_info_keywords"), limit=5),
            "sign_keywords": _normalize_detail_keywords(parsed.get("sign_keywords"), limit=5),
            "structure_keywords": _normalize_detail_keywords(parsed.get("structure_keywords"), limit=5),
            "exception_keywords": _normalize_detail_keywords(parsed.get("exception_keywords"), limit=5),
            "other_info_keywords": _normalize_detail_keywords(parsed.get("other_info_keywords"), limit=5),
            "ornament_keywords": _normalize_detail_keywords(parsed.get("ornament_keywords"), limit=5),
        }
        for key in ("brands", "colors", "materials", "types"):
            if not plan[key]:
                plan[key] = fallback[key]
        for key in (
            "type_info_keywords", "sign_keywords", "structure_keywords",
            "exception_keywords", "other_info_keywords", "ornament_keywords",
        ):
            if not plan[key]:
                plan[key] = fallback[key]
        if not plan.get("plate_text"):
            plan["plate_text"] = fallback["plate_text"]
        return plan
    except Exception:
        return fallback


def search_text(query: str) -> dict[str, Any]:
    started = perf_counter()
    rows, _ = list_vehicle_payloads()
    if not rows:
        return {
            "query": query, "total_candidates": 0,
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            "result_mode": "text_hybrid_rrf",
            "query_plan": {
                "has_plate": None, "brands": [], "colors": [], "materials": [], "types": [], "keywords": [],
                "plate_text": None,
                "type_info_keywords": [], "sign_keywords": [], "structure_keywords": [],
                "exception_keywords": [], "other_info_keywords": [], "ornament_keywords": [],
            },
            "results": [],
        }

    plan = _build_llm_query_plan(query, rows)
    has_plate = plan.get("has_plate")
    brands = _normalize_text_list(plan.get("brands"))
    colors = _normalize_text_list(plan.get("colors"))
    materials = _normalize_text_list(plan.get("materials"))
    types = _normalize_text_list(plan.get("types"))
    keyword_groups = _query_plan_keyword_groups(plan)
    keyword_count = sum(len(v) for v in keyword_groups.values())
    has_structured = isinstance(has_plate, bool) or bool(brands or colors or materials or types)
    has_hard = _has_hard_filters(plan)

    # 无明确条件时不返回全量，避免“语义不明确”导致噪音结果。
    if not has_structured and not keyword_count and not str(plan.get("plate_text") or "").strip():
        return {
            "query": query, "total_candidates": len(rows),
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            "result_mode": "text_hybrid_rrf",
            "query_plan": plan,
            "results": [],
        }

    filtered_structured: list[dict[str, Any]] = []
    for row in rows:
        if not _row_passes_hard_filters(row, plan):
            continue
        if colors and not _match_any_color_family(row.get("color"), colors):
            continue
        if materials and not _match_any_exact(row.get("material"), materials):
            continue
        if types and not _match_any_exact(row.get("type"), types):
            continue

        attr_boost, kw_score = _row_soft_boost(row, plan, keyword_groups)
        if kw_score <= 0 and keyword_count > 0 and not has_structured:
            continue

        item = dict(row)
        item["score"] = float(attr_boost + kw_score)
        filtered_structured.append(item)

    filtered_structured.sort(
        key=lambda x: (float(x.get("score") or 0), str(x.get("updated_at") or ""), int(x.get("vehicle_id") or 0)),
        reverse=True,
    )

    semantic_candidates: list[dict[str, Any]] = []
    semantic_error: str | None = None
    try:
        semantic_raw = _semantic_text_to_image_candidates(query, rows, top_k=120)
        for item in semantic_raw:
            if has_hard and not _row_passes_hard_filters(item, plan):
                continue
            if colors and not _match_any_color_family(item.get("color"), colors):
                continue
            if materials and not _match_any_exact(item.get("material"), materials):
                continue
            if types and not _match_any_exact(item.get("type"), types):
                continue
            attr_boost, kw_score = _row_soft_boost(item, plan, keyword_groups)
            row = dict(item)
            row["semantic_score"] = float(row.get("semantic_score") or 0) + (attr_boost * 0.08) + (kw_score * 0.03)
            semantic_candidates.append(row)
        semantic_candidates.sort(
            key=lambda x: (float(x.get("semantic_score") or 0), str(x.get("updated_at") or ""), int(x.get("vehicle_id") or 0)),
            reverse=True,
        )
    except Exception as exc:
        semantic_error = str(exc)

    final = _fuse_ranked_results(filtered_structured, semantic_candidates)
    if not final:
        final = filtered_structured
        for i, item in enumerate(final, 1):
            item["rank"] = i

    result_mode = "text_hybrid_rrf" if semantic_candidates else "llm_structured"

    return {
        "query": query, "total_candidates": len(rows),
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "result_mode": result_mode,
        "query_plan": plan,
        "semantic_available": bool(semantic_candidates),
        "semantic_error": semantic_error,
        "structured_count": len(filtered_structured),
        "semantic_count": len(semantic_candidates),
        "results": final,
    }


def _safe_int_list(value: Any, limit: int = 50) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    if not isinstance(value, list):
        return out
    for item in value:
        try:
            n = int(item)
        except Exception:
            continue
        if n < 1 or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= limit:
            break
    return out


def _compact_vehicle_row_for_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "vehicle_id": row.get("vehicle_id"),
        "image_name": row.get("image_name"),
        "image_url": row.get("image_url"),
        "type": row.get("type"),
        "type_info": row.get("type_info"),
        "brand": row.get("brand"),
        "color": row.get("color"),
        "material": row.get("material"),
        "sign": row.get("sign"),
        "structure": row.get("structure"),
        "exception": row.get("exception"),
        "has_plate": row.get("has_plate"),
        "plate": row.get("plate"),
        "other_info": row.get("other_info"),
        "score": row.get("score"),
        "semantic_score": row.get("semantic_score"),
        "rank": row.get("rank"),
    }


def _agent_tool_search_vehicles(query: str, top_k: int) -> dict[str, Any]:
    safe_top_k = max(1, min(int(top_k), MODULE1_AGENT_TOOL_TOPK_MAX))
    raw = search_text(query)
    rows = raw.get("results") or []
    compact = [_compact_vehicle_row_for_tool(dict(r)) for r in rows[:safe_top_k]]
    return {
        "query": query,
        "top_k": safe_top_k,
        "result_mode": raw.get("result_mode"),
        "query_plan": raw.get("query_plan"),
        "returned_count": len(compact),
        "items": compact,
    }


def _agent_tool_get_vehicles_by_ids(vehicle_ids: list[int]) -> dict[str, Any]:
    ids = _safe_int_list(vehicle_ids, limit=MODULE1_AGENT_TOOL_TOPK_MAX)
    items: list[dict[str, Any]] = []
    for vid in ids:
        payload = get_vehicle_payload(vid)
        if isinstance(payload, dict):
            items.append(_compact_vehicle_row_for_tool(payload))
    return {
        "vehicle_ids": ids,
        "returned_count": len(items),
        "items": items,
    }


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def _build_agent_final_payload(
    *,
    query: str,
    answer: str,
    vehicle_ids: list[int],
    items: list[dict[str, Any]],
    result_mode: str,
    latency_ms: float,
    debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dedup_ids = _safe_int_list(vehicle_ids, limit=MODULE1_AGENT_TOOL_TOPK_MAX)
    if not items and dedup_ids:
        items = _agent_tool_get_vehicles_by_ids(dedup_ids).get("items") or []
    return {
        "query_id": str(uuid.uuid4()),
        "mode": "text_agent",
        "query": query,
        "answer": answer,
        "vehicle_ids": dedup_ids,
        "returned_count": len(items),
        "items": items,
        "results": items,
        "result_mode": result_mode,
        "latency_ms": round(latency_ms, 2),
        "debug": debug or {},
    }


def agent_chat_retrieve(
    *,
    query: str,
    history: list[dict[str, Any]] | None = None,
    top_k: int = 12,
) -> dict[str, Any]:
    started = perf_counter()
    q = str(query or "").strip()
    if not q:
        raise RuntimeError("query cannot be empty")

    safe_top_k = max(1, min(int(top_k), MODULE1_AGENT_TOOL_TOPK_MAX))

    normalized_history: list[dict[str, str]] = []
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        content = str(turn.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        normalized_history.append({"role": role, "content": content[:1200]})
    normalized_history = normalized_history[-MODULE1_AGENT_HISTORY_LIMIT:]

    # 兜底路径：即使工具调用不可用，也要返回稳定的 IDs 与结果列表。
    def _fallback(reason: str) -> dict[str, Any]:
        raw = search_text(q)
        rows = raw.get("results") or []
        picked = rows[:safe_top_k]
        vehicle_ids = [int(r["vehicle_id"]) for r in picked if isinstance(r.get("vehicle_id"), int)]
        items = [_compact_vehicle_row_for_tool(dict(r)) for r in picked]
        answer = f"已为你检索到 {len(vehicle_ids)} 辆候选车辆。"
        return _build_agent_final_payload(
            query=q,
            answer=answer,
            vehicle_ids=vehicle_ids,
            items=items,
            result_mode="text_agent_fallback",
            latency_ms=(perf_counter() - started) * 1000,
            debug={"fallback_reason": reason, "query_plan": raw.get("query_plan")},
        )

    api_key = get_openrouter_api_key()
    if not api_key:
        return _fallback("api_key_missing")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_vehicles",
                "description": "按用户查询从车辆描述库中检索候选车辆。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "用户检索语句"},
                        "top_k": {"type": "integer", "description": "候选数量，建议 1-30", "minimum": 1, "maximum": MODULE1_AGENT_TOOL_TOPK_MAX},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_vehicles_by_ids",
                "description": "根据车辆 ID 列表获取车辆描述与图片链接。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vehicle_ids": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "车辆 ID 数组",
                        },
                    },
                    "required": ["vehicle_ids"],
                },
            },
        },
    ]

    system_prompt = (
        "你是车辆检索智能体。"
        "你必须优先调用工具从检索库获取信息，不要凭空猜测。"
        "最终只输出一个 JSON 对象，字段固定为："
        "answer(string), vehicle_ids(array<int>), confidence(number 0-1), reason(string)。"
        "vehicle_ids 仅返回真实存在且与问题匹配的 ID，按相关性降序。"
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(normalized_history)
    messages.append({"role": "user", "content": q})

    tool_trace: list[dict[str, Any]] = []

    try:
        client = get_openrouter_client().with_options(timeout=25.0)
    except Exception:
        return _fallback("client_init_failed")

    for _ in range(MODULE1_AGENT_MAX_TOOL_ROUNDS):
        try:
            completion = _chat_completion_create(
                client,
                model=MODULE1_CHAT_MODEL,
                temperature=0.1,
                max_tokens=900,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            return _fallback(f"tool_call_request_failed: {exc}")

        choices = getattr(completion, "choices", None) or []
        if not choices:
            return _fallback("empty_choices")
        assistant_message = choices[0].message
        tool_calls = getattr(assistant_message, "tool_calls", None) or []

        if tool_calls:
            assistant_payload: dict[str, Any] = {
                "role": "assistant",
                "content": _extract_message_text(getattr(assistant_message, "content", "")),
                "tool_calls": [],
            }
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                fn_name = str(getattr(fn, "name", "") or "")
                fn_args_text = str(getattr(fn, "arguments", "") or "{}")
                call_id = str(getattr(tc, "id", "") or f"call_{uuid.uuid4().hex}")

                assistant_payload["tool_calls"].append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": fn_name, "arguments": fn_args_text},
                    }
                )
            messages.append(assistant_payload)

            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                fn_name = str(getattr(fn, "name", "") or "")
                fn_args_text = str(getattr(fn, "arguments", "") or "{}")
                call_id = str(getattr(tc, "id", "") or f"call_{uuid.uuid4().hex}")

                try:
                    fn_args = json.loads(fn_args_text) if fn_args_text else {}
                    if not isinstance(fn_args, dict):
                        fn_args = {}
                except Exception:
                    fn_args = {}

                if fn_name == "search_vehicles":
                    tool_query = str(fn_args.get("query") or q).strip() or q
                    tool_top_k = int(fn_args.get("top_k") or safe_top_k)
                    tool_result = _agent_tool_search_vehicles(tool_query, tool_top_k)
                elif fn_name == "get_vehicles_by_ids":
                    tool_ids = _safe_int_list(fn_args.get("vehicle_ids"), limit=MODULE1_AGENT_TOOL_TOPK_MAX)
                    tool_result = _agent_tool_get_vehicles_by_ids(tool_ids)
                else:
                    tool_result = {"error": f"unknown tool: {fn_name}"}

                tool_trace.append({"tool": fn_name, "args": fn_args, "returned_count": tool_result.get("returned_count")})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            continue

        final_text = _extract_message_text(getattr(assistant_message, "content", "")).strip()
        parsed = extract_first_json_object(final_text)
        if not isinstance(parsed, dict):
            parsed = {
                "answer": final_text or "检索完成。",
                "vehicle_ids": [],
                "confidence": 0.5,
                "reason": "model_output_not_json",
            }

        vehicle_ids = _safe_int_list(parsed.get("vehicle_ids"), limit=safe_top_k)
        if not vehicle_ids:
            vehicle_ids = _safe_int_list(parsed.get("ids"), limit=safe_top_k)

        items = _agent_tool_get_vehicles_by_ids(vehicle_ids).get("items") if vehicle_ids else []
        answer = str(parsed.get("answer") or "检索完成。")

        return _build_agent_final_payload(
            query=q,
            answer=answer,
            vehicle_ids=vehicle_ids,
            items=items,
            result_mode="text_agent_tool_call",
            latency_ms=(perf_counter() - started) * 1000,
            debug={
                "confidence": parsed.get("confidence"),
                "reason": parsed.get("reason"),
                "tool_trace": tool_trace[-8:],
            },
        )

    return _fallback("max_tool_rounds_reached")


# ---------------------------------------------------------------------------
# 以图搜图
# ---------------------------------------------------------------------------

def _extract_image_query_features(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    client = get_openrouter_client()
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prompt = {
        "task": "从车辆图片提取用于检索的结构化条件。",
        "rules": [
            "只返回一个 JSON 对象。",
            "字段仅允许: type, type_info, brand, color, material, sign, structure, exception, has_plate, plate, keywords。",
            "无法确定请返回 null（keywords 为空数组）。",
            "keywords 最多 5 个简短词。",
        ],
        "schema": {
            "type": "string|null",
            "type_info": "string|null",
            "brand": "string|null",
            "color": "string|null",
            "material": "string|null",
            "sign": "string|null",
            "structure": "string|null",
            "exception": "string|null",
            "has_plate": "boolean|null",
            "plate": "string|null",
            "keywords": ["string"],
        },
    }
    completion = _chat_completion_create(
        client,
        model=MODULE1_CHAT_MODEL, temperature=0, max_tokens=220,
        messages=[
            {"role": "system", "content": "你是车辆图片检索条件提取器。"},
            {"role": "user", "content": [
                {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    )
    content = completion.choices[0].message.content
    if isinstance(content, list):
        content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    else:
        content = str(content or "")
    parsed = extract_first_json_object(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("Failed to parse structured image query JSON.")
    has_plate = parsed.get("has_plate")
    if not isinstance(has_plate, bool):
        has_plate = None
    keywords = [str(kw or "").strip() for kw in (parsed.get("keywords") or []) if str(kw or "").strip()]
    def _text_or_none(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None
    return {
        "type": _text_or_none(parsed.get("type")),
        "type_info": _text_or_none(parsed.get("type_info")),
        "brand": _text_or_none(parsed.get("brand")),
        "color": _text_or_none(parsed.get("color")),
        "material": _text_or_none(parsed.get("material")),
        "sign": _text_or_none(parsed.get("sign")),
        "structure": _text_or_none(parsed.get("structure")),
        "exception": _text_or_none(parsed.get("exception")),
        "has_plate": has_plate,
        "plate": _text_or_none(parsed.get("plate")),
        "keywords": keywords[:5],
    }


def search_image_by_embedding(image_bytes: bytes, mime_type: str, top_k: int) -> dict[str, Any]:
    started = perf_counter()
    query_vec = openrouter_image_embedding(image_bytes, mime_type)
    rows, total = list_vehicle_payloads()
    scored: list[dict[str, Any]] = []
    rebuilt, cache_hits = 0, 0
    rebuild_unlimited = MODULE1_MAX_EMBED_REBUILD_PER_SEARCH <= 0
    for row in rows:
        vid = row.get("vehicle_id")
        if not isinstance(vid, int):
            continue
        vec = get_cached_embedding(vid)
        if vec:
            cache_hits += 1
        elif rebuild_unlimited or rebuilt < MODULE1_MAX_EMBED_REBUILD_PER_SEARCH:
            try:
                vec = build_vehicle_image_embedding(row)
                if vec:
                    rebuilt += 1
            except Exception:
                vec = None
        if not vec:
            continue
        item = dict(row)
        item["score"] = float(vector_cosine_similarity(query_vec, vec))
        scored.append(item)
    scored.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    # 过滤相似度 < 0.5 的候选，并限制最多 100 条粗筛结果
    scored = [item for item in scored if float(item.get("score") or 0) >= 0.5]
    final = scored[:min(top_k, 100)]
    for i, item in enumerate(final, 1):
        item["rank"] = i
    return {
        "top_k": top_k, "total_candidates": total,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "result_mode": "image_embedding",
        "embedding_cache_hits": cache_hits, "embedding_rebuilt": rebuilt,
        "embedding_rebuild_limit": MODULE1_MAX_EMBED_REBUILD_PER_SEARCH,
        "results": final,
    }


def search_image_by_structure(
    image_bytes: bytes,
    mime_type: str,
    top_k: int,
    candidate_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    plan = _extract_image_query_features(image_bytes, mime_type)
    if isinstance(candidate_rows, list):
        rows = [dict(r) for r in candidate_rows]
        total = len(rows)
    else:
        rows, total = list_vehicle_payloads()
    scored: list[dict[str, Any]] = []
    for row in rows:
        score = 0.0
        if isinstance(plan.get("has_plate"), bool):
            score += 2.0 if bool(row.get("has_plate")) == bool(plan["has_plate"]) else -0.3
        for key, weight in (("type", 1.3), ("type_info", 0.9), ("brand", 1.0), ("color", 1.0), ("material", 1.0), ("sign", 0.8), ("structure", 0.8), ("exception", 0.8)):
            expected = str(plan.get(key) or "").strip().lower()
            actual = str(row.get(key) or "").strip().lower()
            if expected:
                if actual == expected:
                    score += weight
                elif expected in actual or actual in expected:
                    score += weight * 0.6
                elif _normalize_match_text(expected) and _normalize_match_text(actual):
                    en, an = _normalize_match_text(expected), _normalize_match_text(actual)
                    if en in an or an in en:
                        score += weight * 0.6
        qplate = _normalize_match_text(str(plan.get("plate") or ""))
        rplate = _normalize_match_text(str(row.get("plate") or ""))
        if qplate and rplate and (qplate == rplate or qplate in rplate or rplate in qplate):
            score += 3.0
        text_blob = " ".join([
            str(row.get("type") or ""), str(row.get("brand") or ""),
            str(row.get("type_info") or ""), str(row.get("color") or ""), str(row.get("material") or ""),
            str(row.get("sign") or ""), str(row.get("structure") or ""),
            str(row.get("exception") or ""), str(row.get("plate") or ""),
            str(row.get("other_info") or ""),
        ]).lower()
        for kw in plan.get("keywords") or []:
            if str(kw or "").strip().lower() in text_blob:
                score += 0.8
        if score > 0:
            item = dict(row)
            item["score"] = round(float(score), 4)
            scored.append(item)
    scored.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    final = scored[:top_k]
    for i, item in enumerate(final, 1):
        item["rank"] = i
    return {
        "top_k": top_k, "total_candidates": total,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "result_mode": "image_structured_fallback",
        "query_plan": plan, "results": final,
    }


def search_image_two_stage(
    image_bytes: bytes,
    mime_type: str,
    top_k: int,
    coarse_k: int = 100,
) -> dict[str, Any]:
    """两阶段串行检索：先向量粗检，再结构化细检。"""
    started = perf_counter()
    coarse = search_image_by_embedding(image_bytes=image_bytes, mime_type=mime_type, top_k=coarse_k)
    coarse_results = coarse.get("results") or []

    fine = search_image_by_structure(
        image_bytes=image_bytes,
        mime_type=mime_type,
        top_k=top_k,
        candidate_rows=coarse_results,
    )

    return {
        "top_k": top_k,
        "coarse_k": coarse_k,
        "total_candidates": coarse.get("total_candidates"),
        "latency_ms": round((perf_counter() - started) * 1000, 2),
        "result_mode": "image_two_stage",
        "coarse_results": coarse_results,
        "coarse_returned_count": len(coarse_results),
        "fine_query_plan": fine.get("query_plan"),
        "results": fine.get("results") or [],
        "embedding_cache_hits": coarse.get("embedding_cache_hits"),
        "embedding_rebuilt": coarse.get("embedding_rebuilt"),
        "embedding_rebuild_limit": coarse.get("embedding_rebuild_limit"),
    }


# ---------------------------------------------------------------------------
# VLM 串行分析（逐张调用；失败样本仅记录 failed，不生成占位结果）
# ---------------------------------------------------------------------------

def _analyze_single_for_service(
    image_path: Path,
    model: str,
    api_key: str,
    runtime_cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """分析单张图片，返回 (result_dict_or_None, error_msg_or_None)。"""
    image_name = image_path.name
    vehicle_id = parse_vehicle_id(image_name)
    if not image_path.exists() or not image_path.is_file():
        reason = f"image_not_found: {image_path}"
        return None, reason
    if api_key:
        try:
            cfg = runtime_cfg or {}
            parsed = analyze_with_openrouter(
                image_path=image_path,
                model=model,
                api_key=api_key,
                temperature=float(cfg.get("temperature", 0.0)),
                enable_thinking=bool(cfg.get("enable_thinking", False)),
                max_tokens_fast=int(cfg.get("max_tokens_fast", 2048)),
                max_tokens_retry=int(cfg.get("max_tokens_retry", 2048)),
            )
            result = normalize_result(parsed, image_name=image_name, vehicle_id=vehicle_id, response_mode="vlm")
            return result, None
        except Exception as exc:
            error_msg = str(exc)
            return None, error_msg
    else:
        return None, "api_key_missing"


def run_vlm_analyze_serial(
    images: list[Path],
    model: str = MODULE1_CHAT_MODEL,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """按输入顺序逐张调用 VLM 分析。"""
    def emit_progress(payload: dict[str, Any]) -> None:
        if not progress_cb:
            return
        try:
            progress_cb(payload)
        except Exception:
            return

    api_key = get_openrouter_api_key()
    runtime_cfg = _get_module1_vlm_runtime_config()
    started_at = datetime.now(timezone.utc).isoformat()
    failed: list[dict[str, Any]] = []
    mode_stats: dict[str, int] = {"vlm": 0}

    # 过滤无效文件名
    valid_images: list[Path] = []
    for p in images:
        if parse_vehicle_id(p.name) is None:
            failed.append({"image_name": p.name, "error": "invalid vehicle image name pattern"})
        else:
            valid_images.append(p)

    if not valid_images:
        emit_progress({
            "event": "finished",
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "running": False,
        })
        return {
            "model": model, "selected_count": len(images), "valid_count": 0,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "vlm_runtime": runtime_cfg,
            "response_mode_stats": mode_stats, "results": [], "failed": failed,
        }

    results: list[dict[str, Any]] = []
    success_count = 0
    failed_count = 0
    total_count = len(valid_images)
    emit_progress({
        "event": "started",
        "total": total_count,
        "processed": 0,
        "success": 0,
        "failed": 0,
        "running": True,
    })

    for idx, image_path in enumerate(valid_images, start=1):
        try:
            result, error_msg = _analyze_single_for_service(image_path, model, api_key or "", runtime_cfg)
        except Exception as exc:
            result = None
            error_msg = str(exc)

        if error_msg:
            failed.append({"image_name": image_path.name, "error": error_msg})
            failed_count += 1

        if not result:
            emit_progress({
                "event": "item_done",
                "image_name": image_path.name,
                "total": total_count,
                "processed": idx,
                "success": success_count,
                "failed": failed_count,
                "running": idx < total_count,
            })
            continue

        mode = result.get("response_mode", "vlm")
        mode_stats[mode] = mode_stats.get(mode, 0) + 1
        results.append(result)
        success_count += 1
        emit_progress({
            "event": "item_done",
            "image_name": image_path.name,
            "total": total_count,
            "processed": idx,
            "success": success_count,
            "failed": failed_count,
            "running": idx < total_count,
        })

    emit_progress({
        "event": "finished",
        "total": total_count,
        "processed": total_count,
        "success": success_count,
        "failed": failed_count,
        "running": False,
    })

    return {
        "model": model,
        "selected_count": len(images),
        "valid_count": len(valid_images),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "vlm_runtime": runtime_cfg,
        "response_mode_stats": mode_stats,
        "results": results,
        "failed": failed,
    }
