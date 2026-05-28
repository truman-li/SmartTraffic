"""Module-1 VLM 批量车辆图像分析脚本（并发版）。

用法（CLI）：
    python vlm_batch_analyze.py \\
        --imgs_dir <图片目录> \\
        --output   <输出JSON路径> \\
        [--model   <百炼模型>] \\
        [--image_names <vehicle_1.jpg,vehicle_2.jpg,...>] \\
        [--max_workers <并发线程数，默认 4>]

核心逻辑委托给 utils.py；本脚本只负责参数解析、并发调度和结果汇总。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# 将当前脚本所在目录加入搜索路径，保证 utils 可直接 import
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from utils import (  # noqa: E402
    IMAGE_SUFFIXES,
    VEHICLE_IMAGE_PATTERN,
    analyze_with_openrouter,
    build_fallback_result,
    normalize_result,
    now_iso,
    parse_vehicle_id,
    resolve_openrouter_key,
)


# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Module-1 VLM 车辆图像批量分析（并发）")
    parser.add_argument("--imgs_dir", required=True, help="车辆图片所在目录")
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径")
    parser.add_argument("--model", default="qwen3.6-plus", help="百炼模型名称")
    parser.add_argument("--image_names", default="", help="逗号分隔的图片文件名（留空则扫描全部）")
    parser.add_argument("--max_workers", type=int, default=4, help="并发线程数（默认 4）")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 图片收集
# ---------------------------------------------------------------------------

def collect_images(imgs_dir: Path, image_names_csv: str) -> list[Path]:
    """收集待分析的图片路径，按 vehicle_id 升序排列。"""
    selected_names = [n.strip() for n in image_names_csv.split(",") if n.strip()]
    if selected_names:
        paths: list[Path] = []
        for name in selected_names:
            path = imgs_dir / name
            if path.exists() and path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                paths.append(path)
        paths.sort(key=lambda p: p.name.lower())
        return paths

    rows: list[tuple[int, Path]] = []
    for entry in imgs_dir.iterdir():
        if not entry.is_file() or entry.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        vid = parse_vehicle_id(entry.name)
        key = vid if isinstance(vid, int) else 10 ** 9
        rows.append((key, entry))
    rows.sort(key=lambda item: (item[0], item[1].name.lower()))
    return [item[1] for item in rows]


# ---------------------------------------------------------------------------
# 单张分析任务（供线程池调用）
# ---------------------------------------------------------------------------

def _analyze_one(
    image_path: Path,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    """分析单张图片，返回规范化结果（含 response_mode）。失败时返回 fallback。"""
    image_name = image_path.name
    vehicle_id = parse_vehicle_id(image_name)

    if api_key:
        try:
            parsed = analyze_with_openrouter(
                image_path=image_path,
                model=model,
                api_key=api_key,
            )
            return normalize_result(
                parsed,
                image_name=image_name,
                vehicle_id=vehicle_id,
                response_mode="vlm",
            )
        except Exception as exc:  # noqa: BLE001
            return {
                **build_fallback_result(
                    image_name=image_name,
                    vehicle_id=vehicle_id,
                    reason=f"vlm_error: {exc}",
                ),
                "_error": str(exc),
            }
    else:
        return build_fallback_result(
            image_name=image_name,
            vehicle_id=vehicle_id,
            reason="api_key_missing",
        )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    imgs_dir = Path(args.imgs_dir).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not imgs_dir.exists() or not imgs_dir.is_dir():
        output_path.write_text(
            json.dumps({"error": f"imgs_dir not found: {imgs_dir}"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    images = collect_images(imgs_dir, args.image_names)
    if not images:
        output_path.write_text(
            json.dumps({"error": "no images found", "imgs_dir": str(imgs_dir)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    api_key = resolve_openrouter_key()
    started_at = now_iso()
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    mode_stats: dict[str, int] = {"vlm": 0, "fallback": 0}

    # 过滤掉无效文件名的图片（非 vehicle_N.ext 格式）
    valid_images: list[Path] = []
    for image_path in images:
        if parse_vehicle_id(image_path.name) is None:
            failed.append({"image_name": image_path.name, "error": "invalid vehicle image name pattern"})
        else:
            valid_images.append(image_path)

    max_workers = max(1, min(args.max_workers, len(valid_images) if valid_images else 1))

    # 并发分析：使用 ThreadPoolExecutor
    # 结果映射 future → image_path，保证顺序
    future_to_path: dict[Any, Path] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for image_path in valid_images:
            future = executor.submit(_analyze_one, image_path, args.model, api_key)
            future_to_path[future] = image_path

        # 按完成顺序收集结果，再按 vehicle_id 排序
        raw_results: list[tuple[int, dict[str, Any]]] = []
        for future in as_completed(future_to_path):
            image_path = future_to_path[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                image_name = image_path.name
                vehicle_id = parse_vehicle_id(image_name) or 0
                result = build_fallback_result(image_name=image_name, vehicle_id=vehicle_id, reason=f"thread_error: {exc}")
                result["_error"] = str(exc)

            # 分离内部错误标记
            error_msg = result.pop("_error", None)
            if error_msg:
                failed.append({"image_name": result.get("image_name", image_path.name), "error": error_msg})

            mode = result.get("response_mode", "fallback")
            mode_stats[mode] = mode_stats.get(mode, 0) + 1
            vid_key = result.get("vehicle_id") or 10 ** 9
            raw_results.append((vid_key, result))

    # 按 vehicle_id 升序排列最终结果
    raw_results.sort(key=lambda x: x[0])
    results = [r for _, r in raw_results]

    payload = {
        "model": args.model,
        "imgs_dir": str(imgs_dir),
        "selected_count": len(images),
        "valid_count": len(valid_images),
        "started_at": started_at,
        "finished_at": now_iso(),
        "response_mode_stats": mode_stats,
        "results": results,
        "failed": failed,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
