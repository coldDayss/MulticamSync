"""Local, read-only media discovery and audio/timecode synchronization.

Timeline convention: a source sample at local time t is displayed at
clip['start'] + camera['offset'] + t. A positive offset delays that camera.
Only derived mono audio and waveforms are written to the configured cache.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

FFMPEG = 'ffmpeg'
FFPROBE = 'ffprobe'
CACHE_DIR = Path(__file__).resolve().parent / 'cache'
CANCEL = None
SAMPLE_RATE = 8000
FEATURE_RATE = 100
VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.mkv', '.avi', '.mts', '.m2ts', '.mxf', '.webm'}
COLORS = ['#54d8c3', '#8aabff', '#f9bd66', '#ef92ba', '#bd97f5', '#8bd182', '#ee8677', '#72cce5']


class SyncCancelled(RuntimeError):
    pass


def configure(ffmpeg, ffprobe, cache_dir=None, cancel=None):
    global FFMPEG, FFPROBE, CACHE_DIR, CANCEL
    FFMPEG, FFPROBE = str(ffmpeg), str(ffprobe)
    CANCEL = cancel
    if cache_dir:
        CACHE_DIR = Path(cache_dir)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _run(command, timeout=180):
    if CANCEL is not None and CANCEL.is_set():
        raise SyncCancelled('작업을 취소했습니다.')
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=0x08000000 if os.name == 'nt' else 0) as process:
        try:
            while True:
                if CANCEL is not None and CANCEL.is_set():
                    raise SyncCancelled('작업을 취소했습니다.')
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            process.kill()
            process.communicate()
            raise
    if process.returncode:
        raise RuntimeError(stderr.decode('utf-8', errors='replace')[-3000:])
    return stdout


def _progress(callback, phase, percent, message):
    if CANCEL is not None and CANCEL.is_set():
        raise SyncCancelled('작업을 취소했습니다.')
    if callback:
        callback({'phase': phase, 'progress': round(percent, 1), 'message': message})


def _number(value, default=0.0):
    try:
        if '/' in str(value):
            a, b = str(value).split('/')
            return float(a) / float(b)
        return float(value)
    except (ValueError, TypeError, ZeroDivisionError):
        return default


def _normalize_project(project):
    clips = project['clips']
    origin = min(c['start'] for c in clips)
    if abs(origin) > 1e-9:
        for clip in clips:
            initial_start = clip['start']
            for key in ('start', 'autoStart', 'metadataStart'):
                clip[key] = round(clip.get(key, initial_start)-origin, 6)
    project['duration'] = max(c['start']+c['duration'] for c in clips)


def _timestamp(value):
    if not value:
        return None
    try:
        date = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _filename_timestamp(path):
    match = re.search(r'(?:DJI[_-])?(20\d{12})(?:[_\-.]|$)', Path(path).name)
    if match:
        try:
            # This is a camera clock, not a guaranteed UTC clock. The same
            # convention is used across filenames, preserving relative gaps.
            return dt.datetime.strptime(match.group(1), '%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def parse_timecode(value, fps=30.0):
    """SMPTE wall-clock label to seconds, including 29.97/59.94 drop frame."""
    match = re.fullmatch(r'(\d{1,2}):(\d{2}):(\d{2})([:;])(\d{2,3})', str(value or '').strip())
    if not match:
        return None
    hh, mm, ss, sep, frame = match.groups()
    hh, mm, ss, frame = map(int, (hh, mm, ss, frame))
    nominal = int(round(fps)) or 30
    if hh > 23 or mm > 59 or ss > 59 or frame >= nominal:
        return None
    frames = ((hh * 60 + mm) * 60 + ss) * nominal + frame
    if sep == ';' and nominal in (30, 60) and abs(fps - nominal * 1000 / 1001) < .02:
        dropped = 2 if nominal == 30 else 4
        minutes = hh * 60 + mm
        frames -= dropped * (minutes - minutes // 10)
    return frames / (fps or nominal)


def probe(path):
    raw = json.loads(_run([FFPROBE, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    streams = raw.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise ValueError('영상 스트림이 없습니다.')
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    fmt = raw.get('format', {})
    tags = dict(fmt.get('tags') or {})
    tags.update(video.get('tags') or {})
    for stream in streams:
        if (stream.get('tags') or {}).get('timecode'):
            tags['timecode'] = stream['tags']['timecode']
    fps = _number(video.get('avg_frame_rate')) or _number(video.get('r_frame_rate'), 30)
    duration = _number(video.get('duration')) or _number(fmt.get('duration'))
    timestamp = _timestamp(tags.get('creation_time'))
    source = 'metadata' if timestamp is not None else None
    if timestamp is None:
        timestamp = _filename_timestamp(path)
        source = 'filename' if timestamp is not None else None
    tc = tags.get('timecode')
    tc_seconds = parse_timecode(tc, fps)
    if tc_seconds is None:
        tc = None
    rotation = _number(tags.get('rotate'))
    for side in video.get('side_data_list', []):
        if 'rotation' in side:
            rotation = _number(side['rotation'])
    width, height = int(video.get('width', 0)), int(video.get('height', 0))
    if int(abs(rotation)) % 180 == 90:
        width, height = height, width
    return {'duration': duration, 'fps': fps, 'width': width, 'height': height,
            'hasAudio': bool(audio), 'audioChannels': int((audio or {}).get('channels', 0)),
            'timecode': tc, 'timecodeSeconds': tc_seconds, 'timestamp': timestamp,
            'timestampSource': source, 'creationTime': tags.get('creation_time'),
            'codec': video.get('codec_name'), 'rotation': rotation}


def _camera_name(path, folder):
    relative = Path(path).relative_to(folder)
    for part in list(relative.parts[:-1]) + [relative.stem]:
        match = re.search(r'(?:^|[^A-Za-z0-9])CAM(?:ERA)?[ _-]*([A-Za-z]|\d+)(?=$|[^A-Za-z0-9])', part, re.I)
        if match:
            return 'CAM ' + match.group(1).upper(), True
    if len(relative.parts) > 1:
        return relative.parts[0], False
    return relative.stem, False


def _sort_name(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', value)]


def scan_folder(path, progress=None):
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError('폴더를 찾을 수 없습니다.')
    files = sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS), key=lambda p: str(p).casefold())
    if not files:
        raise ValueError('지원하는 영상 파일이 없습니다. MP4, MOV, MKV, AVI, MTS, M2TS, MXF, WEBM을 지원합니다.')
    project = {'folder': str(folder), 'cameras': [], 'clips': [], 'warnings': [], 'duration': 0}
    names = {}
    inferred = True
    for i, file in enumerate(files):
        _progress(progress, 'scan', i / len(files) * 100, f'영상 정보 읽는 중 {i + 1}/{len(files)} · {file.name}')
        try:
            metadata = probe(file)
            if metadata['duration'] <= 0:
                raise ValueError('영상 길이를 확인할 수 없습니다.')
        except SyncCancelled:
            raise
        except Exception as exc:
            project['warnings'].append(f'{file.name}: 읽기 실패 ({exc})')
            continue
        name, recognized = _camera_name(file, folder)
        inferred = inferred and recognized
        names[name] = None
        clip_id = hashlib.sha256(str(file).encode('utf-8')).hexdigest()[:16]
        project['clips'].append({'id': clip_id, 'cameraName': name, 'path': str(file), 'name': file.name,
                                 'start': 0, 'autoStart': 0, **metadata})
    if not project['clips']:
        raise ValueError('읽을 수 있는 영상이 없습니다. 폴더 및 영상 코덱을 확인해 주세요.')
    for i, name in enumerate(sorted(names, key=_sort_name)):
        names[name] = f'cam-{i+1}'
        project['cameras'].append({'id': names[name], 'name': name, 'color': COLORS[i % len(COLORS)], 'offset': 0.0})
    if not inferred:
        project['warnings'].append('CAM A, CAM B 형식이 없는 영상은 하위 폴더별로 묶고, 최상위 영상은 각각 카메라로 배정했습니다.')
    if len(names) < 2:
        project['warnings'].append('카메라가 1대만 감지되었습니다. 최소 2대의 영상을 CAM A, CAM B 폴더에 넣어 주세요.')
    all_tc = all(c['timecodeSeconds'] is not None for c in project['clips'])
    known = [c['timestamp'] for c in project['clips'] if c['timestamp'] is not None]
    base = min(known) if known else 0
    cursors = {}
    day = 86400.0
    tc_values = [c['timecodeSeconds'] for c in project['clips']] if all_tc else []
    crosses_midnight = bool(tc_values and max(tc_values) - min(tc_values) > day / 2)
    for clip in project['clips']:
        clip['cameraId'] = names[clip.pop('cameraName')]
        if all_tc:
            clock = clip['timecodeSeconds']
            if crosses_midnight and clock < day / 2:
                clock += day
            if clip['timestamp'] is not None and not crosses_midnight:
                # Preserve multiple calendar days, without mixing UTC and TC
                # clock hours. A date only shifts by complete days.
                clock += (math.floor(clip['timestamp'] / day) - math.floor(base / day)) * day
            clip['start'] = clock
            clip['sync'] = {'method': 'timecode', 'confidence': .85, 'note': '임베디드 타임코드 기준. 카메라 타임코드 시계가 같아야 정확합니다.'}
        elif clip['timestamp'] is not None:
            clip['start'] = clip['timestamp'] - base
            label = '촬영 메타데이터' if clip['timestampSource'] == 'metadata' else '파일명 촬영 시각'
            clip['sync'] = {'method': clip['timestampSource'], 'confidence': .25, 'note': f'{label}로 임시 배치 · 오디오 검증 전'}
        else:
            clip['start'] = cursors.get(clip['cameraId'], 0.0)
            clip['sync'] = {'method': 'unconfirmed', 'confidence': 0, 'note': '촬영 시각 없음 · 파일순 임시 배치 · 수동 확인 필요'}
        cursors[clip['cameraId']] = clip['start'] + clip['duration']
    origin = min(c['start'] for c in project['clips'])
    for clip in project['clips']:
        clip['start'] = round(clip['start'] - origin, 6)
        clip['autoStart'] = clip['metadataStart'] = clip['start']
    if all_tc:
        for camera in project['cameras']:
            recorded = sorted((c for c in project['clips'] if c['cameraId'] == camera['id'] and c['timestamp'] is not None), key=lambda c:c['timestamp'])
            for earlier, later in zip(recorded, recorded[1:]):
                discrepancy = (later['start']-earlier['start'])-(later['timestamp']-earlier['timestamp'])
                if abs(discrepancy) > 2:
                    project['warnings'].append(f"{camera['name']}: 클립 사이 타임코드와 촬영 메타데이터 간격이 {abs(discrepancy):.1f}초 다릅니다. 카메라 시계 변경 가능성이 있어 오디오 검증을 권장합니다.")
                    break
    project['duration'] = max(c['start'] + c['duration'] for c in project['clips'])
    project['referenceCameraId'] = project['cameras'][0]['id']
    project['clockBasis'] = 'timecode' if all_tc else 'timestamp' if known else 'unknown'
    _progress(progress, 'scan', 100, f"카메라 {len(names)}대 · 영상 {len(project['clips'])}개 감지")
    return project


def _cache_key(clip, suffix):
    path = Path(clip['path'])
    stat = path.stat()
    token = f'{path}|{stat.st_size}|{stat.st_mtime_ns}|{suffix}'
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / (hashlib.sha256(token.encode('utf-8')).hexdigest()[:32] + '.npy')


def _save_cache(path, data):
    # Waveform preview and automatic sync may request the same source together.
    # Atomic replacement prevents either reader seeing a partial .npy file.
    with tempfile.NamedTemporaryFile(dir=CACHE_DIR, suffix='.npy', delete=False) as handle:
        temporary = handle.name
        np.save(handle, data)
    os.replace(temporary, path)


def _audio(clip):
    key = _cache_key(clip, 'mono8000-v2')
    if key.exists():
        return np.load(key, mmap_mode='r')
    if not clip.get('hasAudio'):
        return np.zeros(0, dtype=np.float32)
    pcm = _run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', clip['path'], '-vn', '-map', '0:a:0',
                '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 'f32le', 'pipe:1'], timeout=max(180, clip['duration'] * 2))
    data = np.frombuffer(pcm, dtype='<f4').copy()
    _save_cache(key, data)
    return data


def _moving_average(values, size):
    size = min(size, len(values))
    if size <= 1:
        return values.copy()
    left = size // 2
    padded = np.pad(values, ((left, size - left - 1), (0, 0)), mode='edge')
    sums = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(padded, axis=0)])
    return (sums[size:] - sums[:-size]) / size


def _features(clip):
    key = _cache_key(clip, 'bands100-v4')
    if key.exists():
        return np.load(key, mmap_mode='r')
    audio = _audio(clip)
    hop, window_size = SAMPLE_RATE // FEATURE_RATE, 320
    count = max(0, 1 + (len(audio) - window_size) // hop)
    feature = np.empty((count, 5), dtype=np.float32)
    window = np.hanning(window_size).astype(np.float32)
    frequencies = np.fft.rfftfreq(window_size, 1 / SAMPLE_RATE)
    ranges = [(80, 300), (300, 700), (700, 1400), (1400, 2600), (2600, 3900)]
    masks = [(frequencies >= a) & (frequencies < b) for a, b in ranges]
    for start in range(0, count, 6000):
        if CANCEL is not None and CANCEL.is_set():
            raise SyncCancelled('작업을 취소했습니다.')
        stop = min(start + 6000, count)
        segment = np.asarray(audio[start*hop:(stop-1)*hop+window_size])
        frames = np.lib.stride_tricks.sliding_window_view(segment, window_size)[::hop][:stop-start]
        spectrum = abs(np.fft.rfft(frames * window, axis=1)) ** 2
        for band, mask in enumerate(masks):
            feature[start:stop, band] = np.log(np.mean(spectrum[:, mask], axis=1) + 1e-9)
    if count:
        feature -= _moving_average(feature, 201).astype(np.float32)
        scale = np.std(feature, axis=0)
        feature /= np.maximum(scale, .15)
        feature = np.clip(feature, -5, 5)
    _save_cache(key, feature)
    return feature


def _normalized_match(search, pattern):
    """Return all valid multiband NCC scores; index is pattern's search start."""
    search = np.asarray(search, dtype=np.float64)
    pattern = np.asarray(pattern, dtype=np.float64)
    n, m = len(search), len(pattern)
    if m < 20 or n < m:
        return np.zeros(0)
    pattern = pattern - np.mean(pattern, axis=0)
    pattern_energy = np.sum(pattern ** 2, axis=0)
    fft_size = 1 << (n + m - 2).bit_length()
    cross = np.fft.irfft(np.fft.rfft(search, fft_size, axis=0) * np.fft.rfft(pattern[::-1], fft_size, axis=0), fft_size, axis=0)[m-1:n]
    sums = np.vstack([np.zeros((1, search.shape[1])), np.cumsum(search, axis=0)])
    squares = np.vstack([np.zeros((1, search.shape[1])), np.cumsum(search ** 2, axis=0)])
    energy = np.maximum(squares[m:] - squares[:-m] - (sums[m:] - sums[:-m]) ** 2 / m, 0)
    scores = cross / np.maximum(np.sqrt(energy * pattern_energy), 1e-12)
    weights = np.minimum(pattern_energy, np.median(pattern_energy) * 2)
    if np.sum(weights) < 1e-6:
        return np.zeros(n-m+1)
    return np.sum(scores * weights, axis=1) / np.sum(weights)


def _refine_raw(ref_audio, target_audio, ref_start, target_start, length=6.0):
    """Refine an established feature match to audio sample precision."""
    margin = .10
    a = max(0, int(round((ref_start - margin) * SAMPLE_RATE)))
    b = int(round(target_start * SAMPLE_RATE))
    size = min(int(length * SAMPLE_RATE), len(target_audio) - b, len(ref_audio) - a - int(2*margin*SAMPLE_RATE))
    if size < SAMPLE_RATE:
        return ref_start - target_start, 0.0
    pattern = np.asarray(target_audio[b:b+size], dtype=np.float64)
    search = np.asarray(ref_audio[a:a+size+int(2*margin*SAMPLE_RATE)], dtype=np.float64)
    # Pre-emphasis suppresses low-frequency wind and engine rumble.
    pattern = np.diff(pattern)[:, None]
    search = np.diff(search)[:, None]
    scores = _normalized_match(search, pattern)
    if not len(scores):
        return ref_start - target_start, 0.0
    index = int(np.argmax(abs(scores)))
    return (a + index) / SAMPLE_RATE - b / SAMPLE_RATE, float(abs(scores[index]))


def _match_pair(reference, target, prior_delta=None):
    ref = _features(reference)
    other = _features(target)
    rdur, tdur = len(ref) / FEATURE_RATE, len(other) / FEATURE_RATE
    if min(rdur, tdur) < 6:
        return {'accepted': False, 'reason': '최소 6초의 유효 오디오가 필요합니다.'}
    if prior_delta is not None:
        low, high = max(0, -prior_delta), min(tdur, rdur-prior_delta)
    else:
        low, high = 0.0, tdur
    if high-low < 6:
        return {'accepted': False, 'reason': '촬영 시각상 겹치는 오디오가 부족합니다.'}
    length = min(12.0, max(3.0, (high-low)/4))
    positions = np.linspace(low, high-length, 5)
    candidates = []
    for position in positions:
        if CANCEL is not None and CANCEL.is_set():
            raise SyncCancelled('작업을 취소했습니다.')
        w = int(round(position * FEATURE_RATE))
        pattern = other[w:w+int(length*FEATURE_RATE)]
        if prior_delta is None:
            search_begin, search_end = 0, len(ref)
        else:
            expected = w/FEATURE_RATE + prior_delta
            search_begin = max(0, int((expected - 45) * FEATURE_RATE))
            search_end = min(len(ref), int((expected + length + 45) * FEATURE_RATE))
        scores = _normalized_match(ref[search_begin:search_end], pattern)
        if not len(scores):
            continue
        best = int(np.argmax(scores))
        score = float(scores[best])
        competing = scores.copy()
        competing[max(0,best-50):min(len(competing),best+51)] = -1
        second = float(np.max(competing)) if len(competing) else -1
        delta = (search_begin + best - w) / FEATURE_RATE
        candidates.append({'delta': delta, 'score': score, 'second': second, 'margin': score-second,
                           'targetTime': w/FEATURE_RATE, 'referenceTime': (search_begin+best)/FEATURE_RATE})
    reliable = [c for c in candidates if c['score'] >= .18 and c['margin'] >= .025]
    clusters = [[other for other in reliable if abs(other['delta']-candidate['delta']) <= .16] for candidate in reliable]
    best_cluster = max(clusters, key=lambda group: (len(group), sum(c['score'] for c in group)), default=[])
    if len(best_cluster) < 2:
        return {'accepted': False, 'reason': '서로 다른 오디오 구간에서 일치하는 지연값을 찾지 못했습니다.', 'windows': candidates}
    # At least two genuinely separate observations, not nearly identical windows.
    if max(c['targetTime'] for c in best_cluster)-min(c['targetTime'] for c in best_cluster) < length * .75:
        return {'accepted': False, 'reason': '독립적인 두 오디오 구간에서 검증되지 않았습니다.', 'windows': candidates}
    average = float(np.mean([c['score'] for c in best_cluster]))
    if len(best_cluster) < 3 and average < .28:
        return {'accepted': False, 'reason': '오디오 상관도가 낮아 자동 확정하지 않았습니다.', 'windows': candidates}
    delta = float(np.median([c['delta'] for c in best_cluster]))
    refined = []
    for candidate in sorted(best_cluster, key=lambda c: -c['score'])[:3]:
        refined_delta, raw_score = _refine_raw(_audio(reference), _audio(target), candidate['referenceTime'], candidate['targetTime'], min(6, length))
        if raw_score >= .08 and abs(refined_delta-delta) <= .14:
            refined.append({'delta': refined_delta, 'score': raw_score})
    if len(refined) >= 2 and np.ptp([c['delta'] for c in refined]) < .06:
        delta = float(np.median([c['delta'] for c in refined]))
    confidence = min(.99, .48 + average*.45 + min(len(best_cluster), 5)*.04)
    return {'accepted': True, 'delta': round(delta,6), 'confidence': round(confidence,3),
            'score': round(average,4), 'matchedWindows': len(best_cluster), 'windows': candidates,
            'rawMatches': refined, 'referenceClipId': reference['id']}


def auto_sync(project, method='auto', progress=None):
    if method not in {'auto', 'audio', 'timecode'}:
        raise ValueError('지원하지 않는 싱크 방식입니다.')
    result = copy.deepcopy(project)
    clips = result['clips']
    if len(result.get('cameras', [])) < 2:
        raise ValueError('동기화하려면 최소 2대의 카메라 영상이 필요합니다.')
    for camera in result['cameras']:
        camera['offset'] = 0.0
    for clip in clips:
        clip['start'] = clip.get('metadataStart', clip.get('autoStart', clip['start']))
        clip['autoStart'] = clip['start']
    if method == 'timecode':
        if result.get('clockBasis') != 'timecode':
            result['warnings'].append('모든 영상에서 임베디드 타임코드를 찾지 못했습니다. 촬영 시각은 타임코드가 아니며, 현재 임시 배치를 유지합니다. 오디오 싱크 또는 수동 조절을 사용해 주세요.')
            for clip in clips:
                clip['sync'] = {'method': 'unconfirmed', 'confidence': 0, 'note': '임베디드 타임코드 없음 · 수동 확인 필요'}
        else:
            for clip in clips:
                clip['sync'] = {'method': 'timecode', 'confidence': .85, 'note': '임베디드 타임코드 기준 · 공통 카메라 시계 전제'}
        result['syncMethod'] = method
        _normalize_project(result)
        _progress(progress, 'sync', 100, '타임코드 검사 완료')
        return result
    ref_id = result.get('referenceCameraId') or result['cameras'][0]['id']
    if not any(c['cameraId'] == ref_id and c.get('hasAudio') for c in clips):
        capable = next((camera for camera in result['cameras'] if any(c['cameraId'] == camera['id'] and c.get('hasAudio') for c in clips)), None)
        if capable is not None:
            ref_id = capable['id']
            result['referenceCameraId'] = ref_id
            result['warnings'].append(f"기존 기준 카메라에 녹음된 소리가 없어 {capable['name']}을 오디오 싱크 기준으로 사용합니다.")
    references = sorted([c for c in clips if c['cameraId'] == ref_id], key=lambda c:c['start'])
    targets = [c for c in clips if c['cameraId'] != ref_id]
    for reference in references:
        basis = '타임코드' if result.get('clockBasis') == 'timecode' else '촬영 시각'
        reference['sync'] = {'method': 'reference', 'confidence': 1.0, 'note': f'기준 카메라 · 여러 클립 사이 간격은 {basis} 기준'}
    successful = 0
    timecode_fallbacks = 0
    for i, target in enumerate(targets):
        _progress(progress, 'sync', i/max(len(targets),1)*100, f"오디오 싱크 {i+1}/{len(targets)} · {target['name']}")
        if not target.get('hasAudio'):
            if method == 'auto' and result.get('clockBasis') == 'timecode':
                target['sync'] = {'method': 'unconfirmed', 'sourceMethod': 'timecode', 'confidence': 0, 'note': '임베디드 타임코드 임시 배치 · 녹음된 소리가 없어 검증 불가 · 수동 확인 필요'}
                timecode_fallbacks += 1
            else:
                target['sync'] = {'method': 'unconfirmed', 'confidence': 0, 'note': '녹음된 소리가 없어 수동 확인 필요'}
            continue
        def overlap(ref):
            return max(0, min(ref['start']+ref['duration'], target['start']+target['duration'])-max(ref['start'],target['start']))
        available = [r for r in references if r.get('hasAudio')]
        ordered = sorted(available, key=lambda r: -overlap(r))
        known_clock = target.get('timestamp') is not None and any(r.get('timestamp') is not None for r in available)
        attempts = []
        match = None
        try:
            for ref in ordered:
                if known_clock and overlap(ref) < 6:
                    continue
                delta = target['start'] - ref['start'] if known_clock else None
                found = _match_pair(ref, target, delta)
                attempts.append({'referenceClipId': ref['id'], **found})
                if found['accepted']:
                    match = (ref, found)
                    break
            # Clock metadata can be wrong by minutes. Try a full audio search
            # against each source, retaining the same multi-window checks.
            if match is None:
                for ref in ordered:
                    if not known_clock and any(a['referenceClipId'] == ref['id'] for a in attempts):
                        continue
                    found = _match_pair(ref, target, None)
                    attempts.append({'referenceClipId': ref['id'], 'wideSearch': True, **found})
                    if found['accepted']:
                        match = (ref, found)
                        break
        except SyncCancelled:
            raise
        except Exception as exc:
            attempts.append({'error': str(exc)})
        if match:
            ref, found = match
            target['start'] = round(ref['start'] + found['delta'], 6)
            target['autoStart'] = target['start']
            correction = target['start'] - target.get('metadataStart', target['start'])
            target['sync'] = {'method': 'audio', 'confidence': found['confidence'],
                              'note': f"오디오 {found['matchedWindows']}개 구간 일치 · 초기 배치 대비 {correction:+.3f}초",
                              'correction': round(correction,6), 'evidence': found}
            if result.get('clockBasis') == 'timecode' and abs(correction) > 2:
                result['warnings'].append(f"{target['name']}: 타임코드 초기 배치와 오디오 결과가 {correction:+.3f}초 달라 오디오로 보정했습니다.")
            successful += 1
        else:
            if method == 'auto' and result.get('clockBasis') == 'timecode':
                target['sync'] = {'method': 'unconfirmed', 'sourceMethod': 'timecode', 'confidence': 0,
                                  'note': '임베디드 타임코드 임시 배치 · 오디오 일치 검증 실패 · 수동 확인 필요', 'attempts': attempts}
                timecode_fallbacks += 1
            else:
                target['sync'] = {'method': 'unconfirmed', 'confidence': 0,
                                  'note': '오디오 일치 검증 실패 · 촬영 시각 임시 배치 유지 · 수동 확인 필요', 'attempts': attempts}
    _normalize_project(result)
    result['syncMethod'] = method
    result['syncSummary'] = {'matched': successful, 'targets': len(targets), 'timecode': timecode_fallbacks, 'unconfirmed': len(targets)-successful}
    if successful < len(targets):
        result['warnings'].append(f'오디오 자동 싱크 {successful}/{len(targets)}개 완료. 미확정 영상은 수동으로 확인해 주세요.')
    result['warnings'] = list(dict.fromkeys(result['warnings']))
    _progress(progress, 'sync', 100, f'오디오 싱크 완료 · {successful}/{len(targets)}개 검증')
    return result


def waveform(clip, points=1200):
    if not clip.get('hasAudio'):
        return {'peaks': [], 'duration': clip['duration']}
    audio = _audio(clip)
    if not len(audio):
        return {'peaks': [], 'duration': clip['duration']}
    boundaries = np.linspace(0, len(audio), min(points,len(audio))+1).astype(int)
    peaks = np.array([np.sqrt(np.mean(np.asarray(audio[a:b],dtype=np.float64)**2)) for a,b in zip(boundaries[:-1],boundaries[1:])])
    normal = max(float(np.percentile(peaks,98)), 1e-6)
    peaks = np.minimum(peaks/normal,1)
    return {'peaks': np.round(peaks,4).tolist(), 'duration': clip['duration']}
