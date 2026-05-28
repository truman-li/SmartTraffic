"""WebUI Backend — FastAPI 聚合入口。

本文件仅负责：
  · FastAPI 应用创建与中间件配置
  · 静态文件挂载（frontend/）
  · 所有 HTTP 路由定义（薄层：仅做参数接收和调用 service 层）
  · 启动事件（init DB、修复图片名、启动 worker）

业务逻辑分别委托给：
  · WebUI/backend/module1_service.py  ← Module-1 车辆管理
  · WebUI/backend/module3_service.py  ← Module-3 GraphRAG
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径设置：确保 WebUI/backend/ 在 sys.path，service 模块可直接 import
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parent.parent   # WebUI/backend/
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_WORKSPACE_DIR = _BACKEND_DIR.parent.parent
_MODULE4_SCRIPTS_DIR = _WORKSPACE_DIR / "Module-4" / "scripts"
if str(_MODULE4_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE4_SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# 导入 service 层
# ---------------------------------------------------------------------------
import module1_service as m1  # noqa: E402
import module2_service as m2  # noqa: E402
import module3_service as m3  # noqa: E402
import trajectory_service as m5  # noqa: E402
import agent_history_service as agent_hist  # noqa: E402

# ---------------------------------------------------------------------------
# 应用与中间件
# ---------------------------------------------------------------------------
app = FastAPI(title="Traffic WebUI API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WORKSPACE_ROOT = m1.WORKSPACE_ROOT
FRONTEND_DIR = WORKSPACE_ROOT / "WebUI" / "frontend"

_module1_progress_lock = Lock()
_module1_vlm_progress: dict[str, Any] = {
    "running": False,
    "job_id": None,
    "total": 0,
    "processed": 0,
    "success": 0,
    "failed": 0,
    "current_image": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _module1_progress_snapshot() -> dict[str, Any]:
    with _module1_progress_lock:
        return dict(_module1_vlm_progress)


def _module1_progress_update(**kwargs: Any) -> None:
    with _module1_progress_lock:
        _module1_vlm_progress.update(kwargs)

# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    method: Literal["local", "global", "drift", "basic"] = "global"
    response_type: str = "Multiple Paragraphs"
    community_level: int | None = None
    verbose: bool = False


class Module1AnalyzeRequest(BaseModel):
    image_names: list[str] = Field(default_factory=list)


class Module1EmbeddingWarmupRequest(BaseModel):
    force: bool = False
    limit: int | None = Field(default=None, ge=1, le=200000)


class Module1AgentTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=1200)


class Module1AgentChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=12, ge=1, le=30)
    history: list[Module1AgentTurn] = Field(default_factory=list)


class Module1BatchDeleteRequest(BaseModel):
    vehicle_ids: list[int] = Field(default_factory=list, max_length=500)


class Module5AskRequest(BaseModel):
    plate_no: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=2000)
    doc_id: str | None = None
    merge_seconds: int = Field(default=0, ge=0, le=3600)


class AgentSessionCreate(BaseModel):
    mode: str = Field(..., min_length=1)
    title: str = Field(default="")
    messages: list[dict[str, Any]] = Field(default_factory=list)


class AgentSessionUpdate(BaseModel):
    title: str | None = None
    messages: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# 启动事件
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    m5.ensure_dirs()
    m3.RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    m1.MODULE1_IMGS_DIR.mkdir(parents=True, exist_ok=True)
    m1.MODULE1_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    m1.MODULE1_VEHICLES_DIR.mkdir(parents=True, exist_ok=True)
    m3.INPUT_DIR.mkdir(parents=True, exist_ok=True)
    m3.INPUT_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    m1.init_db()
    m1.repair_vehicle_image_names()
    m3.ensure_worker_started()
    agent_hist.init_db()


# ---------------------------------------------------------------------------
# 静态文件 & 首页
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


@app.get("/")
def root() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend page not found.")
    return FileResponse(str(index_path))


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    icon_path = FRONTEND_DIR / "intelligence.png"
    if not icon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found.")
    return FileResponse(str(icon_path), media_type="image/png")


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    paths = m3.required_paths_status()
    status = "ok" if all(paths.values()) else "degraded"
    return {
        "status": status,
        "time": datetime.now(timezone.utc).isoformat(),
        "paths": paths,
        "workspace_root": str(WORKSPACE_ROOT),
        "module1_root": str(m1.MODULE1_ROOT),
        "module1_imgs_dir": str(m1.MODULE1_IMGS_DIR),
        "module1_vehicles_dir": str(m1.MODULE1_VEHICLES_DIR),
        "module1_db_file": str(m1.MODULE1_DB_FILE),
        "module3_root": str(m3.MODULE3_ROOT),
        "queue_size": m3.get_queue_size(),
    }


# ---------------------------------------------------------------------------
# Module-1 路由
# ---------------------------------------------------------------------------

@app.post("/api/module1/upload-images")
async def module1_upload_images(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="No image files were provided.")

    dropped_count = 0

    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in m1.MODULE1_IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"Unsupported image format: {upload.filename}")

    uploaded: list[dict[str, Any]] = []
    with m1._name_lock:
        m1.MODULE1_IMGS_DIR.mkdir(parents=True, exist_ok=True)
        used = m1._scan_vehicle_indices()  # public alias for _scan_vehicle_indices
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            target = m1.allocate_vehicle_image_path(used, suffix)
            data = await upload.read()
            target.write_bytes(data)
            await upload.close()
            vehicle_id = m1.parse_vehicle_id_from_image_name(target.name)
            uploaded.append({
                "vehicle_id": vehicle_id,
                "image_name": target.name,
                "image_url": f"/api/module1/image-by-id/{vehicle_id}",
                "saved_path": str(target.relative_to(WORKSPACE_ROOT)).replace("\\", "/"),
                "upload_date": datetime.now(timezone.utc).date().isoformat(),
                "status": "uploaded",
            })
            if isinstance(vehicle_id, int):
                m1.upsert_vehicle_row({"vehicle_id": vehicle_id, "image_name": target.name, "upload_date": datetime.now(timezone.utc).date().isoformat()})

    return {"uploaded_count": len(uploaded), "dropped_count": dropped_count, "items": uploaded}


@app.post("/api/module1/embeddings/warmup")
def module1_warmup_embeddings(payload: Module1EmbeddingWarmupRequest | None = None) -> dict[str, Any]:
    force = bool(payload.force) if payload else False
    limit = payload.limit if payload else None
    try:
        return m1.warmup_vehicle_embeddings(force=force, limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "EMBEDDING_WARMUP_FAILED", "message": "Module-1 embedding warmup failed.", "error": str(exc)},
        ) from exc


@app.post("/api/module1/analyze-vlm")
async def module1_analyze_vlm(payload: Module1AnalyzeRequest | None = None) -> dict[str, Any]:
    """VLM 串行分析：逐张调用 module1_service，无子进程，仅成功结果入库。"""
    selected_names = [n for n in (payload.image_names if payload else []) if isinstance(n, str)]
    images = m1.resolve_vehicle_paths_from_names(selected_names) if selected_names else m1._scan_vehicle_images()
    if not images:
        raise HTTPException(status_code=400, detail="No matching vehicle images found under Module-1/vehicle_imgs.")

    api_key = m1.get_openrouter_api_key()
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "BAILIAN_KEY_MISSING",
                "message": "Bailian API key not found for VLM analyze.",
                "hint": "Set API_KEY in project-root .env (or export DASHSCOPE_API_KEY).",
            },
        )

    with _module1_progress_lock:
        if _module1_vlm_progress.get("running"):
            raise HTTPException(status_code=409, detail="Module-1 VLM analyze is already running.")
        _module1_vlm_progress.update({
            "running": True,
            "job_id": uuid.uuid4().hex,
            "total": len(images),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "current_image": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
        })

    def _progress_cb(data: dict[str, Any]) -> None:
        _module1_progress_update(
            total=int(data.get("total") or _module1_vlm_progress.get("total") or 0),
            processed=int(data.get("processed") or 0),
            success=int(data.get("success") or 0),
            failed=int(data.get("failed") or 0),
            current_image=data.get("image_name"),
            running=bool(data.get("running")),
            finished_at=(datetime.now(timezone.utc).isoformat() if data.get("event") == "finished" else _module1_vlm_progress.get("finished_at")),
        )

    try:
        # 在线程池里顺序执行，不阻塞 event loop
        result_payload = await asyncio.to_thread(
            m1.run_vlm_analyze_serial,
            images,
            m1.MODULE1_CHAT_MODEL,
            _progress_cb,
        )
    except Exception as exc:
        _module1_progress_update(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
        )
        raise

    results = result_payload.get("results") or []
    failed_rows = result_payload.get("failed") or []
    mode_stats = result_payload.get("response_mode_stats") or {}
    success_vlm = int(mode_stats.get("vlm") or 0)
    persisted_count = 0

    if results:
        m1.persist_vehicle_json_files(results)
        persisted_count = len(results)

    _module1_progress_update(
        running=False,
        total=len(images),
        processed=len(images),
        success=success_vlm,
        failed=len(failed_rows),
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    return {
        "processed": len(images),
        "success": success_vlm,
        "success_vlm": success_vlm,
        "result_count": len(results),
        "persisted_count": persisted_count,
        "failed_count": len(failed_rows),
        "first_error": failed_rows[0] if failed_rows else None,
        "failed": failed_rows,
        "selected_images": [p.name for p in images],
        "response_mode_stats": mode_stats,
        "results": results,
    }


@app.get("/api/module1/analyze-vlm/progress")
def module1_analyze_vlm_progress() -> dict[str, Any]:
    return _module1_progress_snapshot()


@app.get("/api/module1/image-by-id/{vehicle_id}")
def module1_get_image_by_id(vehicle_id: int) -> FileResponse:
    if vehicle_id < 1:
        raise HTTPException(status_code=400, detail="Invalid vehicle id.")
    image_path = m1.resolve_vehicle_image_path(vehicle_id)
    if image_path is None:
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(
        str(image_path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/module1/vehicles")
def module1_list_vehicles(
    limit: int = Query(default=200, ge=1, le=1000),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=200),
    type: str | None = Query(default=None),
    color: str | None = Query(default=None),
    material: str | None = Query(default=None),
    has_plate: bool | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> dict[str, Any]:
    rows, total = m1.list_vehicle_payloads(
        limit=limit, page=page, page_size=page_size,
        analyzed_only=False, type_filter=type,
        color_filter=color,
        material_filter=material,
        has_plate=has_plate, date_from=date_from, date_to=date_to,
    )
    return {
        "total": total,
        "page": page or 1,
        "page_size": page_size or min(limit, 200),
        # 兼容新旧前端：老版本读取 vehicles，新版本读取 items
        "items": rows,
        "vehicles": rows,
    }


@app.get("/api/module1/vehicle/{vehicle_id}")
def module1_get_vehicle(vehicle_id: int) -> dict[str, Any]:
    payload = m1.get_vehicle_payload(vehicle_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Vehicle not found.")
    return payload


@app.delete("/api/module1/vehicle/{vehicle_id}")
def module1_delete_vehicle(vehicle_id: int) -> dict[str, Any]:
    if vehicle_id < 1:
        raise HTTPException(status_code=400, detail="Invalid vehicle id.")
    result = m1.delete_vehicle(vehicle_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Vehicle not found.")
    result.pop("found", None)
    return result


@app.post("/api/module1/vehicles/batch-delete")
def module1_batch_delete_vehicles(payload: Module1BatchDeleteRequest) -> dict[str, Any]:
    seen: set[int] = set()
    valid_ids: list[int] = []
    invalid_ids: list[int] = []
    for raw_id in (payload.vehicle_ids or []):
        try:
            vid = int(raw_id)
        except Exception:
            continue
        if vid < 1:
            invalid_ids.append(vid)
            continue
        if vid not in seen:
            seen.add(vid)
            valid_ids.append(vid)
    deleted_items: list[dict[str, Any]] = []
    not_found_ids: list[int] = []
    for vid in valid_ids:
        result = m1.delete_vehicle(vid)
        if result.get("found"):
            result.pop("found", None)
            deleted_items.append(result)
        else:
            not_found_ids.append(vid)
    return {
        "requested_count": len(payload.vehicle_ids or []),
        "processed_count": len(valid_ids),
        "deleted_count": len(deleted_items),
        "not_found_count": len(not_found_ids),
        "invalid_ids": invalid_ids,
        "not_found_ids": not_found_ids,
        "deleted": deleted_items,
    }


@app.post("/api/module1/vehicle/{vehicle_id}/reanalyze")
async def module1_reanalyze_vehicle(vehicle_id: int) -> dict[str, Any]:
    if vehicle_id < 1:
        raise HTTPException(status_code=400, detail="Invalid vehicle id.")
    image_path = m1.resolve_vehicle_image_path(vehicle_id)
    if image_path is None:
        raise HTTPException(status_code=404, detail="Vehicle image not found.")

    result = await module1_analyze_vlm(Module1AnalyzeRequest(image_names=[image_path.name]))
    refreshed = m1.get_vehicle_payload(vehicle_id)
    return {
        "message": "Vehicle info reanalyzed.",
        "vehicle_id": vehicle_id,
        "processed": int(result.get("processed") or 0),
        "success": int(result.get("success") or 0),
        "failed": result.get("failed") or [],
        "response_mode_stats": result.get("response_mode_stats") or {},
        "vehicle": refreshed,
    }


@app.post("/api/module1/chat")
def module1_chat(payload: Module1AgentChatRequest) -> dict[str, Any]:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    history = [{"role": t.role, "content": t.content} for t in (payload.history or [])]

    try:
        raw = m1.agent_chat_retrieve(query=query, history=history, top_k=payload.top_k)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "TEXT_AGENT_FAILED", "message": "Module-1 text agent failed.", "error": str(exc)},
        ) from exc

    m1.log_search(
        "text_agent",
        query,
        {"query": query, "history_len": len(history), "top_k": payload.top_k},
        int(payload.top_k),
        int(raw.get("returned_count") or 0),
        float(raw.get("latency_ms") or 0),
    )
    return raw


@app.post("/api/module1/search/image")
async def module1_search_image(
    file: UploadFile = File(...),
    top_k: int = Query(default=5, ge=1),
) -> dict[str, Any]:
    filename = str(file.filename or "query.jpg")
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in m1.MODULE1_IMAGE_SUFFIXES:
        await file.close()
        raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_IMAGE_FORMAT", "message": f"Unsupported: {filename}"})
    image_bytes = await file.read()
    mime_type = file.content_type or m1.guess_image_mime_type(filename)
    await file.close()
    if not image_bytes:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_IMAGE", "message": "Uploaded image is empty."})

    try:
        coarse_k = max(100, int(top_k))
        raw = m1.search_image_two_stage(image_bytes=image_bytes, mime_type=mime_type, top_k=top_k, coarse_k=coarse_k)
    except Exception as exc:
        err = str(exc)
        err_lower = err.lower()
        code = "IMAGE_TWO_STAGE_FAILED"
        status_code = 502
        if "429" in err or "quota" in err_lower:
            code, status_code = "BAILIAN_QUOTA_EXCEEDED", 429
        elif "api key" in err_lower or "not found" in err_lower:
            code, status_code = "BAILIAN_KEY_MISSING", 401
        elif ("10054" in err_lower) or ("connection reset" in err_lower) or ("forcibly closed" in err_lower) or ("远程主机强迫关闭了一个现有的连接" in err):
            code, status_code = "BAILIAN_CONNECTION_RESET", 503
        raise HTTPException(status_code=status_code, detail={"code": code, "message": "Module-1 image search failed.", "error": err}) from exc

    response = {
        "query_id": str(uuid.uuid4()), "mode": "image", "top_k": top_k,
        "returned_count": len(raw.get("results") or []),
        "results": raw.get("results") or [], "latency_ms": raw.get("latency_ms"),
        "total_candidates": raw.get("total_candidates"), "result_mode": raw.get("result_mode"),
        "query_plan": raw.get("fine_query_plan"),
        "coarse_k": raw.get("coarse_k"),
        "coarse_returned_count": raw.get("coarse_returned_count"),
        "coarse_results": raw.get("coarse_results") or [],
        "embedding_cache_hits": raw.get("embedding_cache_hits"),
        "embedding_rebuilt": raw.get("embedding_rebuilt"),
        "embedding_rebuild_limit": raw.get("embedding_rebuild_limit"),
    }
    m1.log_search("image", None, {"filename": filename, "mime_type": mime_type}, top_k, int(response["returned_count"]), float(raw.get("latency_ms") or 0))
    return response


# ---------------------------------------------------------------------------
# Module-2 路由
# ---------------------------------------------------------------------------

class Module2ReportRequest(BaseModel):
    video_name: str
    yolo_enhance: bool = True
    processed_video_name: str | None = None

@app.post("/api/module2/upload-video")
async def module2_upload_video(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in [".mp4", ".avi", ".mov", ".mkv"]:
        raise HTTPException(status_code=400, detail="Unsupported video format.")
    
    # 获取最大自增 N
    max_n = 0
    if m2.MODULE2_VIDEOS_RAW.exists():
        for child in m2.MODULE2_VIDEOS_RAW.iterdir():
            if child.is_file() and child.name.startswith("video_"):
                try:
                    n = int(child.stem.split("_")[1])
                    if n > max_n:
                        max_n = n
                except (ValueError, IndexError):
                    pass
                    
    save_name = f"video_{max_n + 1}{suffix}"
    save_path = m2.MODULE2_VIDEOS_RAW / save_name
    
    data = await file.read()
    save_path.write_bytes(data)
    await file.close()
    
    return {"success": True, "video_name": save_name}


@app.post("/api/module2/yolo-enhance")
async def module2_yolo_enhance(payload: Module2ReportRequest) -> dict[str, Any]:
    """单独执行 YOLO 增强处理，返回处理后的视频名和路径。"""
    raw_video_path = m2.MODULE2_VIDEOS_RAW / payload.video_name
    if not raw_video_path.exists():
        raise HTTPException(status_code=404, detail="Raw video not found.")

    maybe_processed = await asyncio.to_thread(m2.run_yolo_vehicle_boxes, raw_video_path)
    if isinstance(maybe_processed, Path) and maybe_processed.exists():
        return {
            "success": True,
            "processed_video_name": maybe_processed.name,
            "video_src_dir": "processed",
        }
    return {
        "success": True,
        "processed_video_name": raw_video_path.name,
        "video_src_dir": "raw",
    }


@app.post("/api/module2/analyze-report")
async def module2_analyze_report(payload: Module2ReportRequest) -> dict[str, Any]:
    raw_video_path = m2.MODULE2_VIDEOS_RAW / payload.video_name
    if not raw_video_path.exists():
        raise HTTPException(status_code=404, detail="Raw video not found.")

    yolo_requested = bool(payload.yolo_enhance)
    analyzed_path = raw_video_path
    if yolo_requested:
        preferred_processed_name = str(payload.processed_video_name or "").strip()
        if preferred_processed_name:
            preferred_processed_path = m2.MODULE2_VIDEOS_PROCESSED / preferred_processed_name
            if preferred_processed_path.exists():
                analyzed_path = preferred_processed_path
            else:
                maybe_processed = await asyncio.to_thread(m2.run_yolo_vehicle_boxes, raw_video_path)
                if isinstance(maybe_processed, Path) and maybe_processed.exists():
                    analyzed_path = maybe_processed
        else:
            maybe_processed = await asyncio.to_thread(m2.run_yolo_vehicle_boxes, raw_video_path)
            if isinstance(maybe_processed, Path) and maybe_processed.exists():
                analyzed_path = maybe_processed

    yolo_applied = analyzed_path.parent.name == "processed"

    report_result = await asyncio.to_thread(
        m2.generate_traffic_report,
        analyzed_path,
        yolo_enhanced=yolo_applied,
    )
    if not report_result.get("success"):
        return {"success": False, "error": report_result.get("error") or "大模型分析失败"}

    bundle = m2.save_report_bundle(
        report_markdown=str(report_result.get("report_markdown") or "").strip(),
        raw_video_name=raw_video_path.name,
        analyzed_video_name=analyzed_path.name,
        yolo_enhance_requested=yolo_requested,
        yolo_applied=yolo_applied,
        model=m2.MODULE2_CHAT_MODEL,
    )
    report_id = bundle["report_id"]
    src_dir = "processed" if yolo_applied else "raw"

    return {
        "success": True,
        "video_name": analyzed_path.name,
        "video_src_dir": src_dir,
        "raw_video_name": raw_video_path.name,
        "yolo_enhance_requested": yolo_requested,
        "yolo_applied": yolo_applied,
        "report_id": report_id,
        "report_markdown": report_result.get("report_markdown"),
        "frames_used": report_result.get("frames_used"),
        "download_urls": {
            "md": f"/api/module2/report/{report_id}/download?format=md",
            "txt": f"/api/module2/report/{report_id}/download?format=txt",
            "docx": f"/api/module2/report/{report_id}/download?format=docx",
        },
    }

@app.get("/api/module2/report/{report_id}/download")
def module2_download_report(
    report_id: str,
    format: Literal["md", "txt", "docx"] = Query(default="md"),
) -> FileResponse:
    try:
        file_path, media_type, download_name = m2.resolve_report_download(report_id, format)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(str(file_path), media_type=media_type, filename=download_name)

@app.get("/api/module2/video/raw/{video_name}")
def module2_get_raw_video(video_name: str) -> FileResponse:
    path = m2.MODULE2_VIDEOS_RAW / video_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Raw video not found.")
    return FileResponse(str(path))

@app.get("/api/module2/video/processed/{video_name}")
def module2_get_processed_video(video_name: str) -> FileResponse:
    path = m2.MODULE2_VIDEOS_PROCESSED / video_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Processed video not found.")
    return FileResponse(str(path))


# ---------------------------------------------------------------------------
# Module-3 路由
# ---------------------------------------------------------------------------

@app.post("/api/rules/manual-update")
def manual_update() -> dict[str, Any]:
    m3.validate_runtime_ready()
    task = m3.enqueue_update_task(trigger="manual_retry", uploaded_files=[])
    return {"message": "Manual update has been queued.", "update_task_id": task["task_id"], "update_status": task["status"]}


@app.post("/api/rules/upload-txt")
async def upload_txt(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    m3.validate_runtime_ready()
    if not files:
        raise HTTPException(status_code=400, detail="No files were provided.")

    normalized_names: list[str] = []
    for upload in files:
        normalized_names.append(m3.sanitize_supported_document_filename_or_raise(upload.filename or ""))

    lower_set: set[str] = set()
    for name in normalized_names:
        low = name.lower()
        if low in lower_set:
            raise HTTPException(status_code=400, detail=f"Duplicate file name in request: {name}")
        lower_set.add(low)

    uploaded: list[dict[str, Any]] = []
    with m3.get_name_lock():
        existing_source_lower = {
            p.name.lower()
            for p in m3.INPUT_SOURCES_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in m3.SUPPORTED_UPLOAD_SUFFIXES
        }
        existing_text_lower = {
            p.name.lower()
            for p in m3.INPUT_DIR.iterdir()
            if p.is_file() and p.suffix.lower() == ".txt"
        }
        for name in normalized_names:
            if name.lower() in existing_source_lower:
                raise HTTPException(status_code=409, detail=f"File already exists: {name}")
            if f"{name}.txt".lower() in existing_text_lower:
                raise HTTPException(status_code=409, detail=f"Graph text file already exists for upload: {name}")

        for upload, safe_name in zip(files, normalized_names):
            try:
                data = await upload.read()
                saved = m3.save_uploaded_document_for_graphrag(safe_name, data)
                uploaded.append(saved)
            finally:
                await upload.close()

    task = m3.enqueue_update_task(trigger="auto_after_upload", uploaded_files=[item["original_name"] for item in uploaded])
    return {
        "message": "Upload completed. GraphRAG incremental update has been queued.",
        "uploaded_count": len(uploaded), "uploaded_files": uploaded,
        "update_task_id": task["task_id"], "update_status": task["status"],
    }


@app.get("/api/rules/documents")
def list_documents() -> dict[str, Any]:
    m3.validate_runtime_ready()
    docs = m3.list_uploaded_documents()
    return {"count": len(docs), "documents": docs}


@app.delete("/api/rules/documents")
def delete_documents(payload: dict[str, Any]) -> dict[str, Any]:
    filenames = payload.get("filenames", [])
    if not filenames:
        return {"success": True, "deleted": 0}
        
    deleted = 0
    with m3.get_name_lock():
        for fname in filenames:
            safe_name = m3.sanitize_supported_document_filename_or_raise(fname)
            p_src = m3.INPUT_SOURCES_DIR / safe_name
            if p_src.exists() and p_src.is_file():
                p_src.unlink()
                deleted += 1
            p_txt = m3.INPUT_DIR / f"{safe_name}.txt"
            if p_txt.exists() and p_txt.is_file():
                p_txt.unlink()
                
    return {"success": True, "deleted": deleted}


@app.get("/api/rules/document/{file_name}")
def read_document(file_name: str) -> dict[str, Any]:
    m3.validate_runtime_ready()
    return m3.read_uploaded_document(file_name)


@app.get("/api/rules/update-status/{task_id}")
def update_status(task_id: str) -> dict[str, Any]:
    task = m3.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {**task, "queue_position": m3.get_queue_position(task_id)}


@app.get("/api/rules/tasks/recent")
def recent_tasks(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    records = m3.get_recent_tasks(limit)
    return {"count": len(records), "tasks": records}


@app.post("/api/rules/reset")
def reset_knowledge() -> dict[str, Any]:
    result = m3.reset_knowledge_base()
    return {"success": True, **result}


@app.post("/api/rules/chat")
async def rules_chat(payload: ChatRequest) -> dict[str, Any]:
    m3.validate_runtime_ready()
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    # 异步执行（W-3）：不阻塞 event loop
    return await m3.run_graphrag_query_async(
        question=question,
        method=payload.method,
        response_type=payload.response_type,
        community_level=payload.community_level,
        verbose=payload.verbose,
    )


@app.get("/api/rules/graph-data")
def graph_data(
    max_nodes: int = Query(default=300, ge=10, le=3000),
    max_edges: int = Query(default=600, ge=10, le=6000),
) -> dict[str, Any]:
    m3.validate_runtime_ready()
    return m3.build_graph_data(max_nodes=max_nodes, max_edges=max_edges)


# ---------------------------------------------------------------------------
# Module-5 路由（轨迹重建）
# ---------------------------------------------------------------------------

@app.post("/api/module5/upload-xlsx")
async def module5_upload_xlsx(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")
    suffix = Path(file.filename).suffix.lower()
    if suffix != ".xlsx":
        raise HTTPException(status_code=400, detail="Only .xlsx file is supported.")

    data = await file.read()
    await file.close()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        summary = await asyncio.to_thread(m5.build_dataset_from_upload, file.filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Trajectory dataset parse failed: {exc}") from exc

    return {
        "success": True,
        "message": "Trajectory dataset uploaded and parsed.",
        "summary": summary,
    }


@app.get("/api/module5/documents")
def module5_documents(
    limit: int = Query(default=200, ge=1, le=5000),
) -> dict[str, Any]:
    try:
        return m5.list_uploaded_documents(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Load uploaded documents failed: {exc}") from exc


@app.delete("/api/module5/documents/{doc_id}")
def module5_delete_document(doc_id: str) -> dict[str, Any]:
    try:
        return m5.delete_uploaded_document(doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Delete trajectory document failed: {exc}") from exc


@app.delete("/api/module5/delete-trajectory/{doc_id}")
def module5_delete_document_compat(doc_id: str) -> dict[str, Any]:
    return module5_delete_document(doc_id)


@app.get("/api/module5/summary")
def module5_summary(
    doc_id: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        summary = m5.get_dataset_summary(doc_id=doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError:
        return {
            "doc_id": doc_id,
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Load trajectory summary failed: {exc}") from exc
    return summary


@app.get("/api/module5/plates")
def module5_plates(
    keyword: str = Query(default=""),
    limit: int = Query(default=5000, ge=1, le=20000),
    doc_id: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return m5.list_unique_plates(keyword=keyword, limit=limit, doc_id=doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Load plate list failed: {exc}") from exc


@app.get("/api/module5/trajectory/{plate_no}")
def module5_trajectory(
    plate_no: str,
    merge_seconds: int = Query(default=0, ge=0, le=3600),
    doc_id: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return m5.build_plate_trajectory(plate_no=plate_no, merge_seconds=merge_seconds, doc_id=doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Build trajectory failed: {exc}") from exc


@app.post("/api/module5/ask")
async def module5_ask(payload: Module5AskRequest) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            m5.ask_vehicle_question,
            plate_no=payload.plate_no,
            question=payload.question,
            doc_id=payload.doc_id,
            merge_seconds=payload.merge_seconds,
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ask trajectory failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Agent 历史记录路由
# ---------------------------------------------------------------------------

@app.get("/api/agent/sessions")
def agent_list_sessions(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    items = agent_hist.list_sessions(limit=limit)
    return {"count": len(items), "items": items}


@app.get("/api/agent/sessions/{session_id}")
def agent_get_session(session_id: str) -> dict[str, Any]:
    result = agent_hist.get_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found.")
    return result


@app.post("/api/agent/sessions")
def agent_create_session(payload: AgentSessionCreate) -> dict[str, Any]:
    return agent_hist.create_session(
        mode=payload.mode,
        title=payload.title,
        messages=payload.messages,
    )


@app.put("/api/agent/sessions/{session_id}")
def agent_update_session(session_id: str, payload: AgentSessionUpdate) -> dict[str, Any]:
    result = agent_hist.update_session(
        session_id=session_id,
        title=payload.title,
        messages=payload.messages,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Session not found.")
    return result


@app.delete("/api/agent/sessions/{session_id}")
def agent_delete_session(session_id: str) -> dict[str, Any]:
    ok = agent_hist.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"success": True}


@app.delete("/api/agent/sessions")
def agent_clear_sessions() -> dict[str, Any]:
    count = agent_hist.clear_all_sessions()
    return {"success": True, "deleted_count": count}
