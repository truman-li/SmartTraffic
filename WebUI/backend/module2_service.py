import base64
import io
import json
import logging
import os
import re
import uuid
import zipfile
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

try:
    import cv2
except Exception:
    cv2 = None

try:
    from WebUI.backend.module1_service import (
        WORKSPACE_ROOT,
        _sanitize_ssl_env,
        get_openrouter_api_key,
    )
except Exception:
    from module1_service import (  # type: ignore
        WORKSPACE_ROOT,
        _sanitize_ssl_env,
        get_openrouter_api_key,
    )

logger = logging.getLogger("module2_service")

MODULE2_ROOT = WORKSPACE_ROOT / "Module-2"
MODULE2_VIDEOS_RAW = MODULE2_ROOT / "videos" / "raw"
MODULE2_VIDEOS_PROCESSED = MODULE2_ROOT / "videos" / "processed"
MODULE2_REPORTS_DIR = MODULE2_ROOT / "reports"
MODULE2_DEFAULT_YOLO_MODEL_REL = Path("Module-2") / "model" / "yolov8n.pt"
MODULE2_CHAT_MODEL = "qwen3.6-plus"
MODULE2_CHAT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODULE2_REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")

MODULE2_REPORT_SYSTEM_PROMPT = (
    "你是智能交通监控分析助手。"
    "你的任务是基于视频证据输出专业、客观、可复核的交通监控分析报告。"
    "你必须仅输出报告正文，不要寒暄、不要追问、不要额外解释。"
)

MODULE2_REPORT_TEMPLATE = """
请严格使用以下 Markdown 报告模板输出，并保持标题顺序：

# 交通监控智能分析报告

## 1. 视频概览
- 视频内容概述（场景、天气/光照、道路类型、车流/人流密度）
- 可见度与观测限制（遮挡、模糊、远距、死角）

## 2. 关键参与者识别
- 车辆清单：按“编号-类型-颜色/特征-行为”描述
- 行人/非机动车清单：按“位置-行为-风险点”描述
- 无法确认项明确写“无法从视频确认”

## 3. 关键事件时间线
- 按时间顺序列出关键片段（尽量给出相对时间，如“约第X秒附近”）
- 每条包含：事件、涉及对象、结果

## 4. 疑似违规/异常行为分析
- 列出每项疑似行为：行为描述、证据依据、置信度（高/中/低）
- 仅基于画面证据，不得编造看不见的信息

## 5. 事故与责任初步研判
- 若出现事故：给出可能责任划分（主责/次责/同责/待定）与依据
- 若无明确事故：写“未检出明确事故责任场景”
- 若证据不足：写“责任无法从当前视频单独确认”

## 6. 违规对象画像
- 对每个疑似违规对象给出：类型、颜色、显著特征、运动轨迹、关联事件

## 7. 风险等级与处置建议
- 总体风险等级：低/中/高
- 建议处置：复核重点片段、补充视角、是否建议人工复审

## 8. 结论摘要
- 用 3-6 条要点总结本次分析结果

输出规则：
1) 只输出报告内容本身；
2) 不要出现“您好/以下是/如需我继续”等对话语；
3) 不要虚构车牌、人名、身份；
4) 不确定就明确写“无法从视频确认”。
""".strip()

MODULE2_YOLO_HINT = (
    "补充说明：该视频已进行 YOLO 车辆框选增强，部分车辆已被矩形框高亮。"
    "请将框选区域作为候选关注目标，优先分析框内车辆的轨迹、交互与违规线索，"
    "同时也不要忽略未被框出的关键参与者。"
)

MODULE2_RAW_HINT = (
    "补充说明：该视频为原始画面（未做框选增强）。"
    "请自行在全画面中识别关键车辆/行人并进行分析。"
)

# 确保目录存在
MODULE2_VIDEOS_RAW.mkdir(parents=True, exist_ok=True)
MODULE2_VIDEOS_PROCESSED.mkdir(parents=True, exist_ok=True)
MODULE2_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _reencode_to_h264(video_path: Path) -> Path:
    """使用 imageio (ffmpeg) 将视频转码为 H.264 (yuv420p)，确保浏览器可播放。"""
    try:
        import imageio
        import shutil

        tmp_path = video_path.with_name(f"tmp_{video_path.name}")
        logger.info(f"正在对视频进行浏览器兼容性转码: {video_path.name} ...")

        reader = imageio.get_reader(str(video_path))
        fps = reader.get_meta_data().get("fps", 30)
        writer = imageio.get_writer(
            str(tmp_path),
            fps=fps,
            codec="libx264",
            pixelformat="yuv420p",
            quality=8,
        )
        for frame in reader:
            writer.append_data(frame)
        writer.close()
        reader.close()

        if tmp_path.exists():
            os.remove(video_path)
            shutil.move(str(tmp_path), str(video_path))
            logger.info(f"兼容性转码完成: {video_path.name}")
            return video_path
    except Exception as exc:
        logger.error(f"视频兼容性转码失败 (可能 imageio-ffmpeg 未安装): {exc}")
    return video_path


@lru_cache(maxsize=2)
def _load_yolo_model(model_name: str):
    from ultralytics import YOLO
    return YOLO(model_name)


def _parse_int_env_set(name: str, default_values: list[int]) -> list[int]:
    text = str(os.environ.get(name, "") or "").strip()
    if not text:
        return default_values

    values: list[int] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            values.append(int(piece))
        except Exception:
            continue
    return values or default_values


def _resolve_yolo_model_source() -> str:
    """解析 YOLO 模型来源。

    规则：
    1) 未配置 MODULE2_YOLO_MODEL 时，默认使用相对项目根目录路径 Module-2/model/yolov8n.pt。
    2) 配置为绝对路径时直接使用。
    3) 配置为相对路径且在项目根目录下存在时，解析为绝对路径。
    4) 否则按原字符串处理（兼容 ultralytics 的模型名写法）。
    """
    raw = str(os.environ.get("MODULE2_YOLO_MODEL", "") or "").strip()
    if not raw:
        return str((WORKSPACE_ROOT / MODULE2_DEFAULT_YOLO_MODEL_REL).resolve())

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate)

    workspace_path = (WORKSPACE_ROOT / candidate).resolve()
    if workspace_path.exists():
        return str(workspace_path)

    return raw


def run_yolo_vehicle_boxes(raw_video_path: Path) -> Path | None:
    """运行纯 YOLO 车辆框选增强（仅画框，不显示类别和置信度）。"""
    if cv2 is None:
        logger.warning("OpenCV 不可用，无法执行 YOLO 框选，回退原视频。")
        return raw_video_path

    model_name = _resolve_yolo_model_source()
    class_ids = _parse_int_env_set("MODULE2_YOLO_CLASS_IDS", [2, 5, 7])
    try:
        conf = float(str(os.environ.get("MODULE2_YOLO_CONF", "0.25") or "0.25"))
    except Exception:
        conf = 0.25
    conf = max(0.01, min(0.99, conf))

    try:
        model = _load_yolo_model(model_name)
    except Exception as exc:
        logger.error(f"加载 YOLO 模型失败（{model_name}），回退原视频: {exc}")
        return raw_video_path

    cap = cv2.VideoCapture(str(raw_video_path))
    if not cap.isOpened():
        logger.error(f"无法打开视频文件，回退原视频: {raw_video_path}")
        return raw_video_path

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 25.0

    ok, first_frame = cap.read()
    if not ok or first_frame is None:
        cap.release()
        logger.error(f"无法读取视频首帧，回退原视频: {raw_video_path}")
        return raw_video_path

    height, width = first_frame.shape[:2]
    out_path = MODULE2_VIDEOS_PROCESSED / raw_video_path.name
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        logger.error(f"无法创建输出视频，回退原视频: {out_path}")
        return raw_video_path

    def _draw_boxes(frame):
        try:
            results = model.predict(frame, classes=class_ids, conf=conf, verbose=False)
        except Exception as infer_exc:
            logger.error(f"YOLO 推理失败，当前帧将跳过框选: {infer_exc}")
            return frame

        if not results:
            return frame

        result = results[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return frame

        xyxy = getattr(boxes, "xyxy", None)
        if xyxy is None:
            return frame

        for box in xyxy:
            try:
                x1, y1, x2, y2 = [int(v) for v in box.tolist()[:4]]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (64, 255, 64), 2)
            except Exception:
                continue
        return frame

    frames_written = 0
    try:
        writer.write(_draw_boxes(first_frame))
        frames_written += 1

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            writer.write(_draw_boxes(frame))
            frames_written += 1
    finally:
        writer.release()
        cap.release()

    if frames_written <= 0:
        logger.warning("YOLO 框选未写出有效帧，回退原视频。")
        return raw_video_path

    try:
        out_path = _reencode_to_h264(out_path)
    except Exception as exc:
        logger.error(f"YOLO 输出后置转码失败，保留原输出文件: {exc}")

    return out_path


def extract_frames_as_base64(video_path: Path, max_frames: int = 10) -> list[str]:
    """从视频中均匀提取多帧并转换为 data URL。优先用 cv2，缺失时回退 imageio。"""
    if cv2 is not None:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频文件: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return []

        step = max(1, total_frames // max_frames)
        frames_b64: list[str] = []

        for i in range(max_frames):
            frame_idx = i * step
            if frame_idx >= total_frames:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue

            h, w = frame.shape[:2]
            max_dim = 1024
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)))

            ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                continue
            b64 = base64.b64encode(buffer).decode("ascii")
            frames_b64.append(f"data:image/jpeg;base64,{b64}")

        cap.release()
        return frames_b64

    # cv2 缺失时的回退方案：使用 imageio 提取帧。
    try:
        import imageio.v2 as imageio
    except Exception as exc:
        raise RuntimeError("OpenCV 与 imageio 均不可用，无法抽帧。") from exc

    reader = imageio.get_reader(str(video_path))
    try:
        try:
            total_frames = int(reader.count_frames())
        except Exception:
            total_frames = 0

        targets: set[int]
        if total_frames > 0:
            step = max(1, total_frames // max_frames)
            targets = {i * step for i in range(max_frames) if i * step < total_frames}
        else:
            targets = set(range(max_frames))

        frames_b64: list[str] = []
        for idx, frame in enumerate(reader):
            if idx not in targets:
                continue
            jpg_bytes = imageio.imwrite(imageio.RETURN_BYTES, frame, format="jpg", quality=80)
            b64 = base64.b64encode(jpg_bytes).decode("ascii")
            frames_b64.append(f"data:image/jpeg;base64,{b64}")
            if len(frames_b64) >= max_frames:
                break
        return frames_b64
    finally:
        reader.close()


def _extract_completion_text(content: Any) -> str:
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


def _build_report_prompt(*, yolo_enhanced: bool, focus_hint: str | None = None) -> str:
    hint = MODULE2_YOLO_HINT if yolo_enhanced else MODULE2_RAW_HINT
    focus = ""
    if isinstance(focus_hint, str) and focus_hint.strip():
        focus = f"\n补充关注点（仅在有画面证据时回答）：{focus_hint.strip()}"
    return f"{MODULE2_REPORT_TEMPLATE}\n\n{hint}{focus}".strip()


def _clean_report_text(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)

    greeting_prefix = ("您好", "你好", "当然", "以下是", "下面是")
    if lines and lines[0].strip().startswith(greeting_prefix):
        lines.pop(0)

    tail_noise = re.compile(r"(如需|如果你需要|需要我继续|欢迎继续|可以继续提问)")
    while lines and tail_noise.search(lines[-1]):
        lines.pop()

    cleaned = "\n".join(lines).strip()
    if cleaned and not cleaned.lstrip().startswith("# 交通监控智能分析报告"):
        cleaned = "# 交通监控智能分析报告\n\n" + cleaned
    return cleaned


def generate_traffic_report(video_path: Path, *, yolo_enhanced: bool, focus_hint: str | None = None) -> dict[str, Any]:
    """基于固定提示词生成交通监控分析报告（Markdown）。"""
    api_key = get_openrouter_api_key()
    if not api_key:
        return {"success": False, "error": "未找到百炼 API Key，请在项目根目录 .env 中配置 API_KEY。"}

    if not video_path.exists() or not video_path.is_file():
        return {"success": False, "error": f"视频文件不存在: {video_path}"}

    try:
        from openai import OpenAI

        frames_b64 = extract_frames_as_base64(video_path, max_frames=10)
        if not frames_b64:
            return {"success": False, "error": "无法从视频中提取有效帧。"}

        prompt = _build_report_prompt(yolo_enhanced=yolo_enhanced, focus_hint=focus_hint)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for b64 in frames_b64:
            content.append({"type": "image_url", "image_url": {"url": b64}})

        _sanitize_ssl_env()
        client = OpenAI(
            base_url=MODULE2_CHAT_API_BASE,
            api_key=api_key,
            timeout=150.0,
            default_headers={
                "HTTP-Referer": "http://127.0.0.1:8000",
                "X-DashScope-Sdk": "Traffic-Module2-Report",
            },
        )

        completion = client.chat.completions.create(
            model=MODULE2_CHAT_MODEL,
            temperature=0.1,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": MODULE2_REPORT_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )

        answer = _extract_completion_text(completion.choices[0].message.content)
        report_markdown = _clean_report_text(answer)
        if not report_markdown:
            return {"success": False, "error": "模型未返回有效报告内容。"}

        return {
            "success": True,
            "report_markdown": report_markdown,
            "frames_used": len(frames_b64),
            "model": MODULE2_CHAT_MODEL,
        }
    except Exception as exc:
        import traceback

        trace_str = traceback.format_exc()
        logger.error(f"generate_traffic_report exception: {trace_str}")
        return {"success": False, "error": f"推理执行异常: {exc}", "details": trace_str}


def _markdown_to_plain_text(markdown_text: str) -> str:
    text = str(markdown_text or "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\-\*]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip() + "\n"


def _build_minimal_docx_bytes(text: str) -> bytes:
    """仅依赖标准库生成可打开的最小 .docx 文件。"""
    lines = str(text or "").splitlines()
    paragraph_xml: list[str] = []
    for line in lines:
        if not line.strip():
            paragraph_xml.append("<w:p/>")
            continue
        paragraph_xml.append(
            f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(line)}</w:t></w:r></w:p>"
        )

    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(paragraph_xml)
        + (
            "<w:sectPr>"
            "<w:pgSz w:w=\"11906\" w:h=\"16838\"/>"
            "<w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\" "
            "w:header=\"708\" w:footer=\"708\" w:gutter=\"0\"/>"
            "</w:sectPr>"
        )
        + "</w:body></w:document>"
    )

    content_types_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )

    rels_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/document.xml", document_xml)
    return bio.getvalue()


def save_report_bundle(
    *,
    report_markdown: str,
    raw_video_name: str,
    analyzed_video_name: str,
    yolo_enhance_requested: bool,
    yolo_applied: bool,
    model: str,
) -> dict[str, Any]:
    report_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
    md_path = MODULE2_REPORTS_DIR / f"{report_id}.md"
    txt_path = MODULE2_REPORTS_DIR / f"{report_id}.txt"
    docx_path = MODULE2_REPORTS_DIR / f"{report_id}.docx"
    meta_path = MODULE2_REPORTS_DIR / f"{report_id}.json"

    plain_text = _markdown_to_plain_text(report_markdown)
    md_path.write_text(report_markdown, encoding="utf-8")
    txt_path.write_text(plain_text, encoding="utf-8")
    docx_path.write_bytes(_build_minimal_docx_bytes(plain_text))

    meta = {
        "report_id": report_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_video_name": raw_video_name,
        "analyzed_video_name": analyzed_video_name,
        "yolo_enhance_requested": bool(yolo_enhance_requested),
        "yolo_applied": bool(yolo_applied),
        "model": model,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "report_id": report_id,
        "md_path": md_path,
        "txt_path": txt_path,
        "docx_path": docx_path,
        "meta_path": meta_path,
    }


def resolve_report_download(report_id: str, file_format: str) -> tuple[Path, str, str]:
    rid = str(report_id or "").strip()
    fmt = str(file_format or "").strip().lower()
    if not MODULE2_REPORT_ID_PATTERN.match(rid):
        raise FileNotFoundError("Invalid report id")
    if fmt not in {"md", "txt", "docx"}:
        raise FileNotFoundError("Invalid report format")

    path = MODULE2_REPORTS_DIR / f"{rid}.{fmt}"
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("Report file not found")

    media_type_map = {
        "md": "text/markdown; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    download_name = f"traffic_report_{rid}.{fmt}"
    return path, media_type_map[fmt], download_name


