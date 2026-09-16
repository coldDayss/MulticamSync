"""Frame-aligned local multicamera MP4 export using FFmpeg.

Every camera is rendered over the same frame range. Missing coverage is black
with silent stereo audio. Individual MP4s default to the largest source video's
dimensions for each camera. ``individualResolution='canvas'`` uses the selected
export canvas instead. Sources are letterboxed without stretching. In overlapping source clips, the
earliest clip wins until it ends; later clips resume at the corresponding time.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Callable


class ExportCancelled(RuntimeError):
    pass


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 값이 올바르지 않습니다.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} 값은 유한한 숫자여야 합니다.")
    return result


def _safe_name(value: Any, fallback: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or ""))
    name = name.strip(" .")[:100] or fallback
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name


def _check_cancel(cancel: Any) -> None:
    if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
        raise ExportCancelled("내보내기를 취소했습니다.")


def _notify(callback: Callable | None, fraction: float, message: str) -> None:
    if callback:
        callback({"progress": round(100 * max(0.0, min(1.0, fraction)), 3), "message": message})


def _run_ffmpeg(args: list[str], *, duration: float, begin: float, weight: float,
                message: str, progress: Callable | None, cancel: Any) -> None:
    """Drain both pipes continuously and poll cancellation during encoding."""
    _check_cancel(cancel)
    command = [args[0], "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
               "-progress", "pipe:1", "-nostats", *args[1:]]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    updates: queue.Queue[str] = queue.Queue()
    errors: deque[str] = deque(maxlen=70)

    def read_progress() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            updates.put(line.strip())

    def read_errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            errors.append(line.rstrip())

    readers = [threading.Thread(target=read_progress, daemon=True),
               threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        _notify(progress, begin, message)
        while process.poll() is None or not updates.empty():
            _check_cancel(cancel)
            try:
                line = updates.get(timeout=0.15)
            except queue.Empty:
                continue
            if line.startswith("out_time_us="):
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                _notify(progress, begin + weight * min(1.0, elapsed / duration), message)
        for reader in readers:
            reader.join(timeout=2)
        if process.returncode:
            detail = "\n".join(errors)[-6000:]
            raise RuntimeError(f"FFmpeg 내보내기 실패 ({process.returncode})\n{detail}")
        _notify(progress, begin + weight, message)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def _segments(clips: list[dict], camera: dict, start: float, frames: int,
              fps: float) -> list[dict]:
    """Return nonoverlapping integer-frame coverage, including explicit gaps."""
    offset = _number(camera.get("offset", 0), "카메라 오프셋")
    selected = []
    for clip in clips:
        if str(clip.get("cameraId")) != str(camera["id"]):
            continue
        position = _number(clip.get("start", clip.get("autoStart", 0)), "영상 시작") + offset
        duration = _number(clip.get("duration", 0), "영상 길이")
        if duration <= 0:
            continue
        first = max(0, round((position - start) * fps))
        last = min(frames, round((position + duration - start) * fps))
        if last > first:
            selected.append((position, str(clip.get("id", "")), first, last, clip))
    selected.sort(key=lambda item: (item[0], item[1]))
    result: list[dict] = []
    cursor = 0
    for position, _, first, last, clip in selected:
        first = max(first, cursor)
        if last <= first:
            continue
        if first > cursor:
            result.append({"frames": first - cursor, "clip": None})
        path = Path(str(clip.get("path", "")))
        if not path.is_file():
            raise ValueError(f"원본 영상 파일을 찾을 수 없습니다: {path}")
        result.append({"frames": last - first, "clip": clip,
                       "seek": max(0.0, start + first / fps - position)})
        cursor = last
    if cursor < frames:
        result.append({"frames": frames - cursor, "clip": None})
    return result


def _encode_options(fps: str, frames: int, duration: float) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-r", fps, "-frames:v", str(frames), "-c:a", "aac", "-b:a", "160k",
            "-ar", "48000", "-t", f"{duration:.9f}", "-movflags", "+faststart",
            "-max_muxing_queue_size", "4096", "-threads", "4"]


def _camera_dimensions(clips: list[dict], camera_id: Any,
                       fallback: tuple[int, int]) -> tuple[int, int]:
    """Use the largest source by pixel area; missing metadata uses the canvas."""
    candidates = []
    for clip in clips:
        if str(clip.get("cameraId")) != str(camera_id):
            continue
        if not clip.get("width") or not clip.get("height"):
            continue
        width = _number(clip["width"], "원본 가로 해상도")
        height = _number(clip["height"], "원본 세로 해상도")
        if width != int(width) or height != int(height) or not (1 <= width <= 16384 and 1 <= height <= 16384):
            raise ValueError("원본 영상 해상도가 올바르지 않습니다.")
        # H.264 yuv420p requires even dimensions. Add at most one padding pixel.
        dimensions = (2 * math.ceil(width / 2), 2 * math.ceil(height / 2))
        candidates.append(dimensions)
    return max(candidates, key=lambda size: (size[0] * size[1], size[0]), default=fallback)


def _camera_command(ffmpeg: str, segments: list[dict], width: int, height: int,
                    fps: float, fps_expr: str, frames: int, destination: Path) -> list[str]:
    command = [ffmpeg, "-filter_complex_threads", "1"]
    filters: list[str] = []
    concat: list[str] = []
    input_index = 0
    for index, segment in enumerate(segments):
        count = segment["frames"]
        duration = count / fps
        duration_expr = f"{duration:.9f}"
        clip = segment["clip"]
        if clip is None:
            filters.append(f"color=c=black:s={width}x{height}:r={fps_expr}:d={duration_expr},"
                           f"trim=end_frame={count},setpts=PTS-STARTPTS[v{index}]")
        else:
            command += ["-threads", "2", "-ss", f"{segment['seek']:.9f}", "-t",
                        f"{duration + 2 / fps:.9f}", "-i", str(clip["path"])]
            filters.append(f"[{input_index}:v:0]setpts=PTS-STARTPTS,fps={fps_expr},"
                           f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                           f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={count},"
                           f"setpts=PTS-STARTPTS,format=yuv420p[v{index}]")
        if clip is not None and clip.get("hasAudio", False):
            filters.append(f"[{input_index}:a:0]aresample=48000:async=1:first_pts=0,"
                           "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                           f"apad,atrim=duration={duration_expr},asetpts=PTS-STARTPTS[a{index}]")
        else:
            filters.append("anullsrc=channel_layout=stereo:sample_rate=48000,"
                           f"atrim=duration={duration_expr},asetpts=PTS-STARTPTS[a{index}]")
        concat.append(f"[v{index}][a{index}]")
        if clip is not None:
            input_index += 1
    filters.append("".join(concat) + f"concat=n={len(segments)}:v=1:a=1[vout][aout]")
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                *_encode_options(fps_expr, frames, frames / fps), str(destination)]
    return command


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


def _label_filter(camera: dict, index: int, width: int, height: int, temp: Path) -> str:
    fonts = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf",
             Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font = next((path for path in fonts if path.is_file()), None)
    if font is None or width < 50 or height < 40:
        return ""
    textfile = temp / f"label_{index}.txt"
    label = str(camera.get("name") or camera["id"]).replace("\n", " ").replace("\r", " ")[:48]
    textfile.write_text(label, encoding="utf-8")
    size = max(12, min(28, height // 15, width // max(5, len(label))))
    return (f",drawtext=fontfile='{_filter_path(font)}':textfile='{_filter_path(textfile)}':"
            f"expansion=none:fontcolor=white:fontsize={size}:x=10:y=10:box=1:"
            "boxcolor=black@0.65:boxborderw=5")


def _layout_cells(count: int, layout: str, width: int, height: int) -> list[tuple[int, int, int, int]]:
    if layout == "pip":
        cells = [(0, 0, width, height)]
        gap = max(0, min(8, (height - 24 - 2 * (count - 1)) // (count - 1)))
        thumb_height = max(2, int(min(height * 0.28, (height - 24) / (count - 1) - gap) / 2) * 2)
        thumb_width = max(2, int(thumb_height * width / height / 2) * 2)
        for index in range(count - 1):
            cells.append((width - thumb_width - 12,
                          height - 12 - (count - 1 - index) * (thumb_height + gap),
                          thumb_width, thumb_height))
        return cells
    columns = count if layout == "horizontal" else (1 if layout == "vertical" else math.ceil(math.sqrt(count)))
    rows = math.ceil(count / columns)
    cells = []
    for index in range(count):
        column, row = index % columns, index // columns
        x, y = 2 * (width * column // columns // 2), 2 * (height * row // rows // 2)
        right = 2 * (width * (column + 1) // columns // 2)
        bottom = 2 * (height * (row + 1) // rows // 2)
        cells.append((x, y, right - x, bottom - y))
    return cells


def _combined_command(ffmpeg: str, cameras: list[dict], inputs: list[Path], layout: str,
                      width: int, height: int, fps: float, fps_expr: str, frames: int,
                      audio_camera: str, labels: bool, temp: Path, destination: Path) -> list[str]:
    command = [ffmpeg, "-filter_complex_threads", "1"]
    for source in inputs:
        command += ["-threads", "2", "-i", str(source)]
    cells = _layout_cells(len(cameras), layout, width, height)
    filters = []
    for index, (camera, (_, _, cell_width, cell_height)) in enumerate(zip(cameras, cells)):
        label = _label_filter(camera, index, cell_width, cell_height, temp) if labels else ""
        filters.append(f"[{index}:v:0]scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease:"
                       f"force_divisible_by=2,pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2:"
                       f"color=black,setsar=1,setpts=PTS-STARTPTS{label}[v{index}]")
    if layout == "pip":
        base = "v0"
        for index, (x, y, _, _) in enumerate(cells[1:], 1):
            filters.append(f"[{base}][v{index}]overlay=x={x}:y={y}:shortest=1[p{index}]")
            base = f"p{index}"
        filters.append(f"[{base}]format=yuv420p[vout]")
    else:
        inputs_expr = "".join(f"[v{index}]" for index in range(len(cameras)))
        coordinates = "|".join(f"{x}_{y}" for x, y, _, _ in cells)
        filters.append(f"{inputs_expr}xstack=inputs={len(cameras)}:layout={coordinates}:fill=black,"
                       f"pad={width}:{height}:0:0:color=black,format=yuv420p[vout]")
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio_camera == "none":
        command += ["-an"]
    else:
        audio_index = next(index for index, camera in enumerate(cameras) if str(camera["id"]) == audio_camera)
        command += ["-map", f"{audio_index}:a:0"]
    command += [*_encode_options(fps_expr, frames, frames / fps), str(destination)]
    return command


def _unique_path(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.mp4"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{index}.mp4"
        index += 1
    return candidate


@contextmanager
def _export_workspace(output: Path):
    """Keep the workspace's inherited Windows ACL for FFmpeg child processes.

    Python's TemporaryDirectory creates mode-0700 Windows directories with a
    protected owner-only ACL. Under restricted Windows launchers that can make
    a child-created file readable but impossible for the parent to rename.
    Ordinary mkdir preserves the application's normal inherited permissions.
    """
    parent = output.resolve()
    temporary = parent / (".multicam_export_" + uuid.uuid4().hex)
    temporary.mkdir()
    try:
        yield temporary
    finally:
        # Delete only the directory this invocation created. Never follow a
        # replaced directory link or recursively delete outside the export.
        if temporary.exists():
            if temporary.is_symlink() or temporary.resolve().parent != parent:
                raise RuntimeError("임시 폴더 경로가 변경되어 자동 정리를 중단했습니다.")
            shutil.rmtree(temporary)


def export_project(project: dict, options: dict, output_dir: str | Path,
                   ffmpeg: str | Path, ffprobe: str | Path,
                   progress: Callable | None = None, cancel: Any = None) -> list[Path]:
    """Export combined and/or synchronized per-camera MP4s.

    ``progress`` receives dictionaries with 0..100 ``progress`` and ``message``.
    ``cancel`` may be a threading.Event or a no-argument predicate. Outputs are
    published only after the whole export succeeds, and existing files survive.
    ``individualResolution`` is ``source`` (default, largest clip dimensions per
    camera) or ``canvas`` (the selected combined output width and height).
    Frame rate and timeline duration stay identical across all camera outputs.
    ``ffprobe`` is accepted for the shared engine API; encoding uses ffmpeg.
    """
    _check_cancel(cancel)
    start = _number(options.get("start", 0), "추출 시작")
    end = _number(options.get("end"), "추출 끝")
    fps = _number(options.get("fps", 30), "프레임 속도")
    width_float = _number(options.get("width", 1920), "가로 해상도")
    height_float = _number(options.get("height", 1080), "세로 해상도")
    if not 1 <= fps <= 120:
        raise ValueError("프레임 속도는 1~120 FPS 사이여야 합니다.")
    if end <= start or end - start > 86400:
        raise ValueError("추출 구간은 0초보다 길고 24시간 이하여야 합니다.")
    if any(value != int(value) or int(value) % 2 for value in (width_float, height_float)):
        raise ValueError("해상도는 짝수 정수여야 합니다.")
    width, height = int(width_float), int(height_float)
    if not (128 <= width <= 7680 and 128 <= height <= 7680) or width * height > 7680 * 4320:
        raise ValueError("해상도는 가로·세로 128~7680, 최대 8K 화소 수 이내여야 합니다.")
    frames = round((end - start) * fps)
    if frames < 1:
        raise ValueError("추출 구간이 한 프레임보다 짧습니다.")
    fraction = Fraction(str(fps)).limit_denominator(100000)
    fps_expr = f"{fraction.numerator}/{fraction.denominator}"
    mode = options.get("mode", "combined")
    layout = options.get("layout", "grid")
    individual_resolution = options.get("individualResolution", "source")
    if individual_resolution not in ("source", "canvas"):
        raise ValueError("개별 해상도는 source 또는 canvas를 선택해 주세요.")
    if mode not in ("combined", "individual", "both"):
        raise ValueError("추출 방식은 combined, individual, both 중 하나여야 합니다.")
    if layout not in ("horizontal", "vertical", "grid", "pip"):
        raise ValueError("지원하지 않는 화면 배치입니다.")
    all_cameras = project.get("cameras", [])
    camera_map = {str(camera["id"]): camera for camera in all_cameras}
    if len(camera_map) != len(all_cameras):
        raise ValueError("카메라 ID가 중복되어 있습니다.")
    order = [str(camera_id) for camera_id in (options.get("order") or list(camera_map))]
    if len(order) != len(set(order)) or any(camera_id not in camera_map for camera_id in order):
        raise ValueError("카메라 순서에 중복 또는 존재하지 않는 ID가 있습니다.")
    if not 2 <= len(order) <= 32:
        raise ValueError("카메라를 2대 이상 32대 이하로 선택해 주세요.")
    cameras = [camera_map[camera_id] for camera_id in order]
    audio_camera = str(options.get("audioCamera", order[0]))
    if audio_camera != "none" and audio_camera not in order:
        raise ValueError("오디오 카메라가 추출 카메라에 포함되어 있지 않습니다.")
    executable = str(ffmpeg)
    if not Path(executable).is_file() and not shutil.which(executable):
        raise ValueError("FFmpeg 실행 파일을 찾을 수 없습니다.")
    clips = project.get("clips", [])
    planned_segments = [_segments(clips, camera, start, frames, fps) for camera in cameras]
    if not any(segment["clip"] for segments in planned_segments for segment in segments):
        raise ValueError("선택한 구간에 영상이 없습니다.")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    filename = _safe_name(options.get("filename", "multicam_sync"), "multicam_sync")
    if filename.lower().endswith(".mp4"):
        filename = filename[:-4] or "multicam_sync"
    stages = len(cameras) + int(mode in ("combined", "both"))
    pending: list[tuple[Path, str]] = []
    with _export_workspace(output) as temp:
        rendered: list[Path] = []
        for index, (camera, segments) in enumerate(zip(cameras, planned_segments)):
            target = temp / f"camera_{index}.mp4"
            camera_width, camera_height = (_camera_dimensions(clips, camera["id"], (width, height))
                                           if individual_resolution == "source" else (width, height))
            command = _camera_command(executable, segments, camera_width, camera_height,
                                      fps, fps_expr, frames, target)
            _run_ffmpeg(command, duration=frames / fps, begin=index / stages, weight=1 / stages,
                        message=f"{camera.get('name', camera['id'])} 싱크 구간 렌더링 ({index + 1}/{len(cameras)})",
                        progress=progress, cancel=cancel)
            rendered.append(target)
        if mode in ("combined", "both"):
            target = temp / "combined.mp4"
            command = _combined_command(executable, cameras, rendered, layout, width, height,
                                        fps, fps_expr, frames, audio_camera,
                                        bool(options.get("labels", True)), temp, target)
            _run_ffmpeg(command, duration=frames / fps, begin=len(cameras) / stages, weight=1 / stages,
                        message="통합 화면 렌더링", progress=progress, cancel=cancel)
            pending.append((target, f"{filename}_combined"))
        if mode in ("individual", "both"):
            for camera, source in zip(cameras, rendered):
                camera_name = _safe_name(camera.get("name", camera["id"]), "camera")
                pending.append((source, f"{filename}_{camera_name}"))
        _check_cancel(cancel)
        deliverables = []
        for source, name in pending:
            target = _unique_path(output, name)
            shutil.move(str(source), str(target))
            deliverables.append(target)
    _notify(progress, 1.0, f"내보내기 완료: {len(deliverables)}개 파일")
    return deliverables
