import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import VIDEO_DIR


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,nb_frames:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    try:
        frames = int(stream.get("nb_frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    return {
        "duration_seconds": round(duration, 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "video_frame_count": frames,
    }


def keyframe_count(duration):
    if duration <= 3.5:
        return 3
    if duration <= 6.5:
        return 4
    if duration <= 10.5:
        return 5
    if duration <= 15.5:
        return 6
    if duration <= 18.5:
        return 7
    return 8


def extract_keyframes(path, video_relative_path, duration):
    count = keyframe_count(duration)
    frame_dir = path.parent / f"{path.stem}_keyframes"
    shutil.rmtree(frame_dir, ignore_errors=True)
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(count):
        timestamp = duration * (index + 1) / (count + 1)
        filename = f"frame_{index + 1:02d}.jpg"
        destination = frame_dir / filename
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
                "-vf", "scale='min(360,iw)':-2", "-q:v", "3", str(destination),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        frames.append(
            {
                "relative_path": (Path(video_relative_path).parent / frame_dir.name / filename).as_posix(),
                "time": round(timestamp, 3),
            }
        )
    return frames


def analyze_video(path, relative_path):
    metadata = probe_video(path)
    metadata["keyframes"] = extract_keyframes(
        path, relative_path, metadata["duration_seconds"]
    )
    return metadata


def concat_videos(paths, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as concat_file:
        for path in paths:
            concat_file.write(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        concat_file.flush()
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_file.name,
                "-c", "copy", "-movflags", "+faststart", str(destination),
            ],
            capture_output=True,
            check=True,
            timeout=300,
        )
    return probe_video(destination)


def remove_video_assets(relative_path, keyframes=None):
    path = (VIDEO_DIR / relative_path).resolve()
    if VIDEO_DIR.resolve() in path.parents:
        path.unlink(missing_ok=True)
        shutil.rmtree(path.parent / f"{path.stem}_keyframes", ignore_errors=True)
    for frame in keyframes or []:
        frame_path = (VIDEO_DIR / frame.get("relative_path", "")).resolve()
        if VIDEO_DIR.resolve() in frame_path.parents:
            frame_path.unlink(missing_ok=True)
