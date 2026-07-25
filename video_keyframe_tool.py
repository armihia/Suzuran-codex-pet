from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "video_workspace"
WORKSPACE.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MAX_CANDIDATES = 800

app = Flask(__name__, static_folder=str(ROOT), static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024


def json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def session_dir(session_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(session_id))
    except ValueError as exc:
        raise FileNotFoundError("无效的任务编号") from exc
    path = WORKSPACE / normalized
    if not path.is_dir():
        raise FileNotFoundError("任务不存在或已被删除")
    return path


def load_metadata(path: Path) -> dict[str, Any]:
    with (path / "metadata.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_jpeg(frame: np.ndarray, path: Path) -> None:
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("候选帧编码失败")
    encoded.tofile(str(path))


def safe_output_stem(value: str) -> str:
    stem = Path(value.strip()).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return stem or "character_sequence"


def unique_output_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for version in range(2, 1000):
        alternate = directory / f"{stem}_v{version}{suffix}"
        if not alternate.exists():
            return alternate
    raise RuntimeError("输出目录中同名文件过多，请修改输出文件名")


def sample_background(image: Image.Image) -> tuple[int, int, int]:
    rgb = np.asarray(image.convert("RGB"))
    h, w = rgb.shape[:2]
    patch = max(2, min(h, w) // 50)
    samples = np.concatenate(
        [
            rgb[:patch, :patch].reshape(-1, 3),
            rgb[:patch, w - patch :].reshape(-1, 3),
            rgb[h - patch :, :patch].reshape(-1, 3),
            rgb[h - patch :, w - patch :].reshape(-1, 3),
        ],
        axis=0,
    )
    return tuple(int(value) for value in np.median(samples, axis=0))


def foreground_mask(image: Image.Image) -> np.ndarray:
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    magenta = (r > 170) & (b > 115) & (g < 125) & ((r + b) > (g * 3.2))
    return ~magenta


def find_bounds(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)


def body_anchor_x(mask: np.ndarray, bounds: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = bounds
    h, w = mask.shape
    character_h = y1 - y0
    center = (x0 + x1) / 2
    region_x0 = max(0, int(center - w * 0.11))
    region_x1 = min(w, int(center + w * 0.11))
    region_y0 = max(0, int(y0 + character_h * 0.48))
    region = mask[region_y0:y1, region_x0:region_x1]
    if region.size == 0 or not region.any():
        return center

    weights = np.linspace(1.0, 2.5, region.shape[0], dtype=np.float32)[:, None]
    column_weights = (region * weights).sum(axis=0)
    total = float(column_weights.sum())
    if total <= 0:
        return center
    index = int(np.searchsorted(np.cumsum(column_weights), total / 2))
    return float(region_x0 + index)


def crop_with_padding(
    image: Image.Image,
    left: int,
    top: int,
    width: int,
    height: int,
    fill: tuple[int, int, int],
) -> Image.Image:
    canvas = Image.new("RGB", (width, height), fill)
    src_left = max(0, left)
    src_top = max(0, top)
    src_right = min(image.width, left + width)
    src_bottom = min(image.height, top + height)
    if src_right <= src_left or src_bottom <= src_top:
        return canvas
    fragment = image.crop((src_left, src_top, src_right, src_bottom)).convert("RGB")
    canvas.paste(fragment, (src_left - left, src_top - top))
    return canvas


def fit_to_cell(
    image: Image.Image,
    cell_width: int,
    cell_height: int,
    fill: tuple[int, int, int],
) -> Image.Image:
    source = image.convert("RGB")
    scale = min(cell_width / source.width, cell_height / source.height)
    size = (
        max(1, round(source.width * scale)),
        max(1, round(source.height * scale)),
    )
    resized = source.resize(size, Image.Resampling.LANCZOS)
    cell = Image.new("RGB", (cell_width, cell_height), fill)
    cell.paste(resized, ((cell_width - size[0]) // 2, (cell_height - size[1]) // 2))
    return cell


def compose_shared_crop(
    images: list[Image.Image],
    cell_width: int,
    cell_height: int,
) -> list[Image.Image]:
    masks = [foreground_mask(image) for image in images]
    bounds = [find_bounds(mask) for mask in masks]
    if any(bound is None for bound in bounds):
        return [
            fit_to_cell(image, cell_width, cell_height, sample_background(image))
            for image in images
        ]

    valid_bounds = [bound for bound in bounds if bound is not None]
    padding = max(8, round(images[0].height * 0.035))
    global_top = max(0, min(bound[1] for bound in valid_bounds) - padding)
    global_bottom = min(
        images[0].height, max(bound[3] for bound in valid_bounds) + padding
    )
    crop_height = max(1, global_bottom - global_top)
    crop_width = min(
        images[0].width,
        max(bound[2] - bound[0] for bound in valid_bounds) + padding * 2,
    )
    anchors = [
        body_anchor_x(mask, bound)
        for mask, bound in zip(masks, valid_bounds, strict=True)
    ]

    cells: list[Image.Image] = []
    for image, anchor in zip(images, anchors, strict=True):
        fill = sample_background(image)
        left = round(anchor - crop_width / 2)
        crop = crop_with_padding(
            image, left, global_top, crop_width, crop_height, fill
        )
        cells.append(fit_to_cell(crop, cell_width, cell_height, fill))
    return cells


@app.get("/")
def home():
    return send_from_directory(ROOT, "video_tool.html")


@app.get("/api/config")
def config():
    default_output = ROOT / "output_sequences"
    default_output.mkdir(exist_ok=True)
    return jsonify({"ok": True, "default_output_directory": str(default_output)})


@app.post("/api/choose-output-directory")
def choose_output_directory():
    payload = request.get_json(silent=True) or {}
    initial = str(payload.get("initial", "")).strip()
    initial_path = Path(initial) if initial else ROOT
    if not initial_path.is_dir():
        initial_path = ROOT
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            title="选择角色帧序列输出文件夹",
            initialdir=str(initial_path),
            mustexist=True,
        )
        root.destroy()
    except Exception as exc:
        return json_error(f"无法打开文件夹选择窗口：{exc}", 500)
    return jsonify({"ok": True, "directory": selected})


@app.post("/api/extract")
def extract_frames():
    uploaded = request.files.get("video")
    if uploaded is None or not uploaded.filename:
        return json_error("请选择一个视频文件")

    extension = Path(uploaded.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return json_error("不支持该视频格式，请使用 MP4、MOV、AVI、MKV 或 WebM")

    try:
        interval = int(request.form.get("interval", "1"))
    except ValueError:
        return json_error("抽帧间隔必须是整数")
    if interval < 1:
        return json_error("抽帧间隔不能小于 1")

    session_id = str(uuid.uuid4())
    task_dir = WORKSPACE / session_id
    candidates_dir = task_dir / "candidates"
    output_dir = task_dir / "output"
    candidates_dir.mkdir(parents=True)
    output_dir.mkdir()

    video_path = task_dir / f"source{extension}"
    uploaded.save(video_path)

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return json_error("视频无法读取，可能是编码格式不受支持")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames > 0 and math.ceil(total_frames / interval) > MAX_CANDIDATES:
        capture.release()
        return json_error(
            f"预计会产生超过 {MAX_CANDIDATES} 张候选图，请增大抽帧间隔"
        )

    candidates: list[dict[str, Any]] = []
    frame_index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % interval == 0:
            if len(candidates) >= MAX_CANDIDATES:
                capture.release()
                return json_error(
                    f"候选图超过 {MAX_CANDIDATES} 张，请增大抽帧间隔"
                )
            filename = f"frame_{frame_index:08d}.jpg"
            save_jpeg(frame, candidates_dir / filename)
            candidates.append(
                {
                    "id": filename,
                    "frame_index": frame_index,
                    "time_seconds": round(frame_index / fps, 4) if fps > 0 else None,
                    "url": f"/api/session/{session_id}/candidate/{filename}",
                }
            )
        frame_index += 1
    capture.release()

    if not candidates:
        return json_error("没有从视频中读取到有效帧")

    metadata = {
        "session_id": session_id,
        "original_name": uploaded.filename,
        "fps": fps,
        "total_frames": total_frames or frame_index,
        "interval": interval,
        "candidates": candidates,
    }
    with (task_dir / "metadata.json").open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, **metadata})


@app.get("/api/session/<session_id>/candidate/<filename>")
def candidate_image(session_id: str, filename: str):
    try:
        task_dir = session_dir(session_id)
        metadata = load_metadata(task_dir)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return json_error(str(exc), 404)
    valid_names = {item["id"] for item in metadata["candidates"]}
    if filename not in valid_names:
        return json_error("候选帧不存在", 404)
    return send_from_directory(task_dir / "candidates", filename)


@app.post("/api/compose")
def compose_sequence():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", ""))
    selected = payload.get("selected", [])
    if not isinstance(selected, list):
        return json_error("选择数据无效")

    try:
        expected_count = int(payload.get("expected_count", len(selected)))
        cell_width = int(payload.get("cell_width", 192))
        cell_height = int(payload.get("cell_height", 208))
    except (TypeError, ValueError):
        return json_error("帧数或单帧尺寸无效")

    if len(selected) != expected_count:
        return json_error(f"请选择恰好 {expected_count} 张候选帧")
    if not 1 <= len(selected) <= 32:
        return json_error("合成帧数必须在 1–32 之间")
    if not 32 <= cell_width <= 1024 or not 32 <= cell_height <= 1024:
        return json_error("单帧尺寸必须在 32–1024 像素之间")

    try:
        task_dir = session_dir(session_id)
        metadata = load_metadata(task_dir)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return json_error(str(exc), 404)

    candidates_by_id = {item["id"]: item for item in metadata["candidates"]}
    if any(item_id not in candidates_by_id for item_id in selected):
        return json_error("选择中包含无效候选帧")

    ordered = sorted(
        (candidates_by_id[item_id] for item_id in selected),
        key=lambda item: item["frame_index"],
    )
    images = [
        Image.open(task_dir / "candidates" / item["id"]).convert("RGB")
        for item in ordered
    ]
    mode = str(payload.get("mode", "shared_crop"))
    if mode == "shared_crop":
        cells = compose_shared_crop(images, cell_width, cell_height)
    else:
        cells = [
            fit_to_cell(image, cell_width, cell_height, sample_background(image))
            for image in images
        ]

    sequence = Image.new("RGB", (cell_width * len(cells), cell_height))
    for index, cell in enumerate(cells):
        sequence.paste(cell, (index * cell_width, 0))

    output_stem = safe_output_stem(str(payload.get("output_name", "")))
    output_name = f"{output_stem}_{len(cells)}f_{cell_width}x{cell_height}.png"
    output_path = task_dir / "output" / output_name
    sequence.save(output_path, "PNG", optimize=True)

    manifest = {
        "source_video": metadata["original_name"],
        "source_fps": metadata["fps"],
        "extraction_interval": metadata["interval"],
        "selected": ordered,
        "frame_count": len(cells),
        "cell_width": cell_width,
        "cell_height": cell_height,
        "canvas_width": cell_width * len(cells),
        "canvas_height": cell_height,
        "alignment": mode,
        "output": output_name,
    }
    manifest_path = task_dir / "output" / f"{output_name}.json"
    with manifest_path.open(
        "w", encoding="utf-8"
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    requested_directory = str(payload.get("output_directory", "")).strip()
    external_directory = (
        Path(requested_directory).expanduser()
        if requested_directory
        else ROOT / "output_sequences"
    )
    try:
        external_directory.mkdir(parents=True, exist_ok=True)
        if not external_directory.is_dir():
            return json_error("指定的输出位置不是文件夹")
        external_png = unique_output_path(external_directory, output_name)
        external_json = external_png.with_suffix(external_png.suffix + ".json")
        shutil.copy2(output_path, external_png)
        shutil.copy2(manifest_path, external_json)
    except (OSError, RuntimeError) as exc:
        return json_error(f"无法写入指定输出位置：{exc}")

    return jsonify(
        {
            "ok": True,
            "download_url": f"/api/session/{session_id}/output/{output_name}",
            "manifest_url": (
                f"/api/session/{session_id}/output/{output_name}.json"
            ),
            "width": sequence.width,
            "height": sequence.height,
            "selected": ordered,
            "saved_path": str(external_png),
            "manifest_saved_path": str(external_json),
        }
    )


@app.get("/api/session/<session_id>/output/<filename>")
def output_file(session_id: str, filename: str):
    try:
        task_dir = session_dir(session_id)
    except FileNotFoundError as exc:
        return json_error(str(exc), 404)
    output_dir = task_dir / "output"
    path = output_dir / filename
    if path.parent != output_dir or not path.is_file():
        return json_error("输出文件不存在", 404)
    return send_file(path, as_attachment=True, download_name=filename)


def open_frontend() -> None:
    webbrowser.open("http://127.0.0.1:8765")


if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(0.9, open_frontend).start()
    app.run(host="127.0.0.1", port=8765, debug=False)
