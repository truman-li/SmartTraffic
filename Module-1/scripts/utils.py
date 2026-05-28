"""Module-1 共享工具库。

本模块提供：
- JSON 对象提取
- MIME 类型推断
- API Key 解析（百炼优先）
- 车辆类型规范化与字段推断
- VLM 图片识别核心调用（含质量重试）
- numpy 向量余弦相似度
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VEHICLE_IMAGE_PATTERN = re.compile(r"^vehicle_(\d+)\.(jpg|jpeg|png|bmp|webp)$", re.IGNORECASE)

MAX_TOKENS_FAST = 2048
MAX_TOKENS_RETRY = 2048

ALLOWED_VEHICLE_TYPES = [
    "货车", "客车", "工程车辆", "特殊车辆",
    "SUV", "小轿车", "其他车型",
]

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def guess_mime_type(image_name: str) -> str:
    """根据文件名后缀推断 MIME 类型。"""
    suffix = Path(image_name).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".bmp":
        return "image/bmp"
    return "image/jpeg"


def parse_vehicle_id(image_name: str) -> int | None:
    """从 vehicle_N.ext 文件名中解析车辆 ID。"""
    match = VEHICLE_IMAGE_PATTERN.match(image_name)
    if match is None:
        return None
    return int(match.group(1))


def _sanitize_ssl_env_for_http() -> None:
    """清理无效证书环境变量，避免 httpx/openai 报 [Errno 2] No such file or directory。"""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        val = os.getenv(var)
        if val and not Path(val).exists():
            os.environ.pop(var, None)


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个完整 JSON 对象（支持嵌套结构）。"""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    loaded = json.loads(text[start : i + 1])
                except Exception:
                    return None
                return loaded if isinstance(loaded, dict) else None
    return None


# ---------------------------------------------------------------------------
# API Key 解析
# ---------------------------------------------------------------------------

def resolve_openrouter_key(dotenv_path: Path | None = None) -> str | None:
    """按优先级解析 API Key（百炼优先，兼容历史 OpenRouter 变量）。

    顺序：
    1. 环境变量 DASHSCOPE_API_KEY / API_KEY / BAILIAN_API_KEY / GRAPHRAG_API_KEY / OPENROUTER_API_KEY
    2. dotenv_path 指定的 .env 文件（默认尝试项目根目录 .env）
    """
    for env_name in ("DASHSCOPE_API_KEY", "API_KEY", "BAILIAN_API_KEY", "GRAPHRAG_API_KEY", "OPENROUTER_API_KEY"):
        value = os.getenv(env_name)
        if value:
            return value

    # 自动推断项目根 .env 路径（脚本位于 Module-1/scripts/，上两级为项目根）
    if dotenv_path is None:
        try:
            dotenv_path = Path(__file__).resolve().parents[2] / ".env"
        except Exception:
            dotenv_path = None

    if dotenv_path and dotenv_path.exists():
        try:
            for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                k = key.strip()
                if k in {"DASHSCOPE_API_KEY", "API_KEY", "BAILIAN_API_KEY", "GRAPHRAG_API_KEY", "OPENROUTER_API_KEY"}:
                    token = value.strip().strip('"').strip("'")
                    if token:
                        return token
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# 车辆类型 / 字段规范化
# ---------------------------------------------------------------------------

def normalize_vehicle_type(value: Any) -> str:
    """将模型返回的任意格式车辆类型规范化为枚举值。"""
    text = str(value or "").strip()
    if not text:
        return "其他车型"
    if text in ALLOWED_VEHICLE_TYPES:
        return text

    lowered = text.lower()
    rules: list[tuple[list[str], str]] = [
        (["sedan", "轿车", "三厢", "两厢", "car"], "小轿车"),
        (["suv", "越野"], "SUV"),
        (["mpv", "商务车", "面包车", "保姆车", "小巴", "中巴", "客车", "大巴", "公交", "bus", "校车"], "客车"),
        (["货车", "轻卡", "重卡", "牵引车", "挂车", "半挂", "厢式", "厢货", "van", "pickup", "皮卡", "渣土车"], "货车"),
        (["工程", "矿用", "挖掘", "装载", "压路", "吊车"], "工程车辆"),
        (["应急", "救护", "消防", "警车", "抢险", "危险品", "危化", "油罐", "槽罐", "环卫", "垃圾车", "洒水车", "清扫车"], "特殊车辆"),
        (["摩托", "电动车", "三轮", "自行车", "跑车", "超跑", "sport", "sports car"], "其他车型"),
    ]
    for keys, mapped in rules:
        for token in keys:
            token_low = token.lower()
            if token in text or token_low in lowered:
                return mapped
    return "其他车型"


def infer_fields_from_name(image_name: str) -> tuple[str | None, str | None]:
    """从文件名关键词中推断车辆类型和颜色（用于 fallback）。"""
    lowered = image_name.lower()

    color = None
    if "white" in lowered or "白" in image_name:
        color = "白色"
    elif "black" in lowered or "黑" in image_name:
        color = "黑色"
    elif "blue" in lowered or "蓝" in image_name:
        color = "蓝色"
    elif "red" in lowered or "红" in image_name:
        color = "红色"

    vtype = None
    if any(t in lowered for t in ("pickup", "皮卡")):
        vtype = "货车"
    elif any(t in lowered for t in ("truck", "货车", "light_truck", "轻卡", "小货")):
        vtype = "货车"
    elif any(t in lowered for t in ("bus", "客车", "大巴", "公交")):
        vtype = "客车"
    elif any(t in lowered for t in ("工程", "挖机", "装载", "压路", "吊车")):
        vtype = "工程车辆"
    elif any(t in lowered for t in ("警车", "消防", "救护", "危化", "危险品", "环卫")):
        vtype = "特殊车辆"
    elif any(t in lowered for t in ("suv", "越野")):
        vtype = "SUV"
    elif any(t in lowered for t in ("sedan", "轿车", "car")):
        vtype = "小轿车"
    elif any(t in lowered for t in ("摩托", "电动", "三轮")):
        vtype = "其他车型"

    return vtype, color


# ---------------------------------------------------------------------------
# 结果构建与规范化
# ---------------------------------------------------------------------------

def _normalize_has_plate_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "有", "是"}:
            return True
        if lowered in {"false", "0", "no", "n", "无", "否"}:
            return False
    return None


def _normalize_plate_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_plate_logic_conflicting(payload: dict[str, Any]) -> bool:
    has_plate = _normalize_has_plate_value(payload.get("has_plate"))
    plate = _normalize_plate_value(payload.get("plate"))
    return has_plate is True and plate is None


def _needs_retry_for_quality(payload: dict[str, Any]) -> bool:
    if _is_plate_logic_conflicting(payload):
        return True
    return not has_core_info(payload)


def has_core_info(payload: dict[str, Any]) -> bool:
    """检查结果是否包含至少一项有效核心字段。"""
    for key in ("type", "type_info", "brand", "color", "material", "sign", "structure", "exception", "plate", "other_info"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value is not None and not isinstance(value, str):
            return True
    return False


def build_fallback_result(
    image_name: str,
    vehicle_id: int | None,
    reason: str,
) -> dict[str, Any]:
    """API 调用失败时构建基于文件名推断的 fallback 结果。"""
    inferred_type, inferred_color = infer_fields_from_name(image_name)
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "vehicle_id": vehicle_id,
        "image_name": image_name,
        "type": normalize_vehicle_type(inferred_type),
        "type_info": None,
        "brand": None,
        "color": inferred_color,
        "material": None,
        "sign": None,
        "structure": None,
        "exception": None,
        "has_plate": None,
        "plate": None,
        "upload_date": today,
        "other_info": f"fallback: {reason}",
        "response_mode": "fallback",
    }


def normalize_result(
    raw: dict[str, Any],
    image_name: str,
    vehicle_id: int | None,
    response_mode: str,
) -> dict[str, Any]:
    """将 VLM 原始 JSON 输出规范化为统一字段结构。"""
    out = dict(raw)
    normalized_vehicle_id = vehicle_id if isinstance(vehicle_id, int) else parse_vehicle_id(image_name)
    out["vehicle_id"] = normalized_vehicle_id
    out["image_name"] = image_name  # 始终以输入文件名为准

    has_plate = _normalize_has_plate_value(out.get("has_plate"))
    plate = _normalize_plate_value(out.get("plate"))

    # 强制逻辑一致：有 plate 文本则 has_plate=True；has_plate=True 但无 plate 则修为 False
    if plate is not None:
        has_plate = True
    elif has_plate is True:
        has_plate = False

    upload_date = out.get("upload_date")
    if not isinstance(upload_date, str) or not upload_date.strip():
        upload_date = datetime.now(timezone.utc).date().isoformat()

    type_text = normalize_vehicle_type(out.get("type"))

    type_info_text = out.get("type_info")
    type_info_text = type_info_text.strip() if isinstance(type_info_text, str) and type_info_text.strip() else None

    brand_text = out.get("brand")
    brand_text = brand_text.strip() if isinstance(brand_text, str) and brand_text.strip() else None

    color_text = out.get("color")
    color_text = color_text.strip() if isinstance(color_text, str) and color_text.strip() else None

    material_text = out.get("material")
    material_text = material_text.strip() if isinstance(material_text, str) and material_text.strip() else None

    sign_text = out.get("sign")
    sign_text = sign_text.strip() if isinstance(sign_text, str) and sign_text.strip() else None

    structure_text = out.get("structure")
    structure_text = structure_text.strip() if isinstance(structure_text, str) and structure_text.strip() else None

    exception_text = out.get("exception")
    exception_text = exception_text.strip() if isinstance(exception_text, str) and exception_text.strip() else None

    other_info = out.get("other_info")
    other_info = other_info.strip() if isinstance(other_info, str) and other_info.strip() else None

    return {
        "vehicle_id": out["vehicle_id"],
        "image_name": out["image_name"],
        "type": type_text,
        "type_info": type_info_text,
        "brand": brand_text,
        "color": color_text,
        "material": material_text,
        "sign": sign_text,
        "structure": structure_text,
        "exception": exception_text,
        "has_plate": has_plate,
        "plate": plate,
        "upload_date": upload_date,
        "other_info": other_info,
        "response_mode": response_mode,
    }


# ---------------------------------------------------------------------------
# VLM 调用核心（百炼 OpenAI 兼容）
# ---------------------------------------------------------------------------

def analyze_with_openrouter(
    image_path: Path,
    model: str,
    api_key: str,
    *,
    temperature: float = 0.0,
    enable_thinking: bool = False,
    max_tokens_fast: int = MAX_TOKENS_FAST,
    max_tokens_retry: int = MAX_TOKENS_RETRY,
) -> dict[str, Any]:
    """调用百炼兼容 Chat Completions 分析单张车辆图片，返回规范化前的原始 JSON dict。

    - 优先使用 multimodal 格式；失败时 fallback 到 text+data_url 格式。
    - 输出质量不足（车牌逻辑矛盾、核心字段缺失）时自动追加一次重试。
    - 两种格式均失败才抛出 RuntimeError。
    """
    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("openai package not available") from exc

    _sanitize_ssl_env_for_http()

    client = OpenAI(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=api_key,
        timeout=40,
        default_headers={
            "HTTP-Referer": "http://127.0.0.1:8000",
            "X-DashScope-Sdk": "Traffic-Module1-VLM-Analyze",
        },
    )

    if not image_path.exists() or not image_path.is_file():
        raise RuntimeError(f"image file not found: {image_path}")
    try:
        raw = image_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"image file not found: {image_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"image file read failed: {exc}") from exc
    data_url = f"data:{guess_mime_type(image_path.name)};base64,{base64.b64encode(raw).decode('ascii')}"

    system_prompt = (
        "你是车辆图像结构化助手。"
        "只输出一个JSON对象，不要输出多余文字。"
        "只识别一辆最主要车辆。"
        "type 必须从以下枚举中选择其一："
        "货车、客车、工程车辆、特殊车辆、SUV、小轿车、其他车型。"
        "仅在确实无法判断时才可使用其他车型。"
        "plate 仅允许返回非空字符串或 null，不允许空字符串。"
        "当 has_plate=true 时，plate 必须是可见车牌字符串。"
        "当看不到车牌或无法确认时，has_plate=false 且 plate=null。"
        "所有描述必须客观，仅描述可见事实，不要主观推断或判责。"
        "字段: vehicle_id,image_name,type,type_info,brand,color,material,sign,structure,exception,has_plate,plate,other_info。"
    )
    user_payload = {
        "instruction": (
            "请识别图片中的主要车辆并返回结构化JSON，无法确定填null。"
            "type 只能从给定枚举里选；"
            "type_info 填详细车型描述；"
            "color 填车辆主色；"
            "material 填车身材质；"
            "sign 填可见文字/标志/贴纸/告示；"
            "structure 填特殊结构；"
            "exception 填损伤或改装等异常；"
            "严守规则：has_plate=true 必须给出可见plate；"
            "若无车牌或看不清则 has_plate=false 且 plate=null。"
            "other_info 填其它细节（如反光板、车灯特征）。"
        ),
        "example": {
            "vehicle_id": 12,
            "image_name": "vehicle_12.jpg",
            "type": "货车",
            "type_info": "重型半挂牵引车+罐式挂车",
            "brand": None,
            "color": "银灰色",
            "material": "金属",
            "sign": "车身有危险品标识与安全告示，可见‘易燃液体’字样",
            "structure": "罐体顶部有金属梯子，后部有软管",
            "exception": None,
            "has_plate": True,
            "plate": "鲁BQW69挂",
            "other_info": "后部可见红黄反光板与组合尾灯。",
        },
        "schema": {
            "vehicle_id": "int|null",
            "image_name": "string",
            "type": "enum<货车|客车|工程车辆|特殊车辆|SUV|小轿车|其他车型>",
            "type_info": "string|null",
            "brand": "string|null",
            "color": "string|null",
            "material": "string|null",
            "sign": "string|null",
            "structure": "string|null",
            "exception": "string|null",
            "has_plate": "boolean|null",
            "plate": "string|null",
            "other_info": "string|null",
        },
    }

    last_error: Exception | None = None

    try:
        temperature_value = float(temperature)
    except Exception:
        temperature_value = 0.0
    if temperature_value < 0.0:
        temperature_value = 0.0
    if temperature_value > 1.0:
        temperature_value = 1.0

    try:
        fast_tokens = max(64, int(max_tokens_fast))
    except Exception:
        fast_tokens = MAX_TOKENS_FAST
    try:
        retry_tokens = max(64, int(max_tokens_retry))
    except Exception:
        retry_tokens = MAX_TOKENS_RETRY
    if retry_tokens < fast_tokens:
        retry_tokens = fast_tokens

    def run_once(messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any] | None:
        request_kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature_value,
            "max_tokens": max_tokens,
            "messages": messages,
            "extra_body": {"enable_thinking": bool(enable_thinking)},
        }
        try:
            completion = client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            err_text = str(exc).lower()
            # 兼容不支持 enable_thinking 的网关：自动去掉参数再试一次。
            if (
                "enable_thinking" in err_text
                or "extra_body" in err_text
                or "unexpected keyword argument" in err_text
            ) and "extra_body" in request_kwargs:
                request_kwargs.pop("extra_body", None)
                completion = client.chat.completions.create(**request_kwargs)
            else:
                raise
        choices = getattr(completion, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("model returned empty choices")

        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None

        # 兼容字典结构，避免 NoneType 下标/属性错误。
        if content is None and isinstance(first_choice, dict):
            msg_obj = first_choice.get("message")
            if isinstance(msg_obj, dict):
                content = msg_obj.get("content")

        if content is None:
            raise RuntimeError("model returned empty content")

        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            content_text = "\n".join(parts)
        else:
            content_text = str(content or "")
        return extract_first_json_object(content_text)

    # 尝试两种消息格式：multimodal（优先），然后 plain text + data_url
    message_variants: list[list[dict[str, Any]]] = [
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False) + "\nimage_data_url:" + data_url,
            },
        ],
    ]

    for messages in message_variants:
        try:
            parsed = run_once(messages, fast_tokens)
            if isinstance(parsed, dict) and not _needs_retry_for_quality(parsed):
                return parsed

            # 质量不足时追加一次重试
            if isinstance(parsed, dict):
                retry_messages = [
                    messages[0],
                    {
                        "role": "user",
                        "content": (
                            "请重新识别并修正："
                            "0) type 必须从给定枚举中选；"
                            "1) has_plate=true 时必须提供非空 plate；"
                            "2) 若无车牌或看不清，has_plate=false 且 plate=null；"
                            "3) 不允许 plate 为空字符串。"
                            "4) 尽量补全 type_info、color、material、sign、structure、exception。"
                            "5) other_info 必须客观，仅写可见细节。只返回 JSON。"
                        ),
                    },
                    messages[1],
                ]
                retried = run_once(retry_messages, retry_tokens)
                if isinstance(retried, dict) and not _needs_retry_for_quality(retried):
                    return retried
                if isinstance(retried, dict):
                    return retried
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if last_error is not None:
        raise RuntimeError(str(last_error)) from last_error
    raise RuntimeError("model returned empty or invalid JSON")


# ---------------------------------------------------------------------------
# numpy 向量余弦相似度（M1-3 优化）
# ---------------------------------------------------------------------------

def normalize_embedding(values: list[Any]) -> list[float]:
    """过滤并转换嵌入向量中的数值元素。"""
    out: list[float] = []
    for item in values:
        try:
            out.append(float(item))
        except Exception:
            continue
    return out


def vector_cosine_similarity(a: list[float], b: list[float]) -> float:
    """使用 numpy 计算两个向量的余弦相似度，性能优于纯 Python 循环。

    · 维度不同时取较短维度。
    · 任意向量为空或零向量时返回 0.0。
    """
    if not a or not b:
        return 0.0
    try:
        import numpy as np  # numpy 随 openai/pandas 等库一并安装
        va = np.array(a, dtype=np.float64)
        vb = np.array(b, dtype=np.float64)
        # 对齐维度
        dim = min(len(va), len(vb))
        va, vb = va[:dim], vb[:dim]
        norm = float(np.linalg.norm(va)) * float(np.linalg.norm(vb))
        if norm <= 0.0:
            return 0.0
        return float(np.dot(va, vb) / norm)
    except ImportError:
        # numpy 不可用时降级为纯 Python
        dim = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(dim))
        norm_a = math.sqrt(sum(x * x for x in a[:dim]))
        norm_b = math.sqrt(sum(x * x for x in b[:dim]))
        if norm_a <= 0.0 or norm_b <= 0.0:
            return 0.0
        return dot / (norm_a * norm_b)
