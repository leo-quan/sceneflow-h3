import json
import shutil
import time
import uuid
from pathlib import Path

import httpx
from websockets.sync.client import connect as ws_connect

from config import COMFY_HTTP, COMFY_OUTPUT_DIR, COMFY_WS, VIDEO_DIR


TASK_TYPE = "r2v — 参考主体生视频(Reference to Video)"
WEBSOCKET_MAX_SIZE = 64 * 1024 * 1024


def frame_count(duration):
    frames = max(5, int(duration) * 24)
    while frames % 17 != 5:
        frames += 1
    return frames


def common_prompt(project, reference_count):
    refs = "<Picture 1>"
    if reference_count > 1:
        refs += " 和 <Picture 2>"
    identity = (
        f"{refs} 是同一个人的身份参考。<Picture 1> 是主要面部参考。"
        + ("<Picture 2> 补充侧脸、发型和体型信息。" if reference_count > 1 else "")
        + "所有片段只能出现同一位主角，严格保持脸型、五官比例、肤色、年龄、发际线、发型和体型一致。"
        "不得换脸、复制主角、改变年龄或把参考图理解成不同人物。"
    )
    return "\n\n".join(
        value.strip()
        for value in (
            identity,
            project["character_prompt"],
            "【固定场景与环境】\n" + project["environment_prompt"],
            "【连续性负面约束：必须避免】\n" + project.get("continuity_negative_prompt", ""),
            "【统一视觉】\n" + project["visual_prompt"],
            "【声音】\n" + project["audio_prompt"],
            "禁止多手多指、肢体畸形、面部变形、人物穿模、瞬移、动作倒放、字幕、水印、Logo和乱码文字。",
        )
        if value.strip()
    )


def segment_prompt(segment, project_reference_count, source_segment=None):
    segment_refs = segment.get("references") or []
    refs = []
    if segment_refs:
        refs.append("当前片段的局部约束图优先于全局参考图。" + "".join(f"<Picture {ref['slot']}> 对应片段图片{ref['slot']}，用于约束其中的人物、物品或环境；" for ref in segment_refs))
        if project_reference_count:
            start = 6
            refs.append(f"<Picture {start}>" + (f" 和 <Picture {start + 1}>" if project_reference_count > 1 else "") + " 是全局主角身份参考。")
    elif project_reference_count:
        refs.append("<Picture 1> 是唯一主角。")
    if segment["continuity_mode"] == "continue" and source_segment is not None:
        bridge = (
            f"这是片段{source_segment['position'] + 1}《{source_segment['title']}》的直接续拍。"
            "从来源片段最后一帧和最后动作的下一瞬间开始，继承人物位置、朝向、"
            "步态、表情、服装、摄影机高度与距离、180度轴线、光线、背景布局、风向、音乐和环境声。"
            "禁止重复上一段动作、重新入场、淡入、黑场、闪白或无理由换机位。"
        )
    else:
        bridge = (
            "这是新的镜头，不继承上一段的最后动作和摄影机位置，但必须保持同一人物、服装、世界设定、"
            "时间、天气、场景材质、视觉风格和声音风格。使用自然明确的电影硬切。"
        )
    ending = segment["ending_prompt"].strip() or (
        "最后1秒让主角保持清晰可见并处于未完成的自然动作中，为下一段留下可续拍状态；禁止定格、淡出和黑场。"
    )
    world = segment.get("world_prompt", "").strip()
    world_override = f"\n\n【本片段世界设定：优先级高于全局世界设定】\n{world}\n如与全局世界设定冲突，必须以本片段世界设定为准。" if world else ""
    return f"{' '.join(refs)}\n{bridge}{world_override}\n\n【本段剧情】\n{segment['prompt'].strip()}\n\n【结尾续拍状态】\n{ending}".strip()


def build_timeline(project, references, segments, selected_position):
    refs = [
        {"index": ref["slot"] - 1, "imageFile": ref["comfy_path"], "imageB64": ""}
        for ref in references
    ]
    total = sum(seg["frame_count"] for seg in segments)
    start = 0
    timeline_segments = []
    index_by_id = {segment["id"]: index for index, segment in enumerate(segments)}
    segment_by_id = {segment["id"]: segment for segment in segments}
    for seg in segments:
        length = seg["frame_count"]
        segment_refs = seg.get("references") or []
        if segment_refs:
            refs_for_segment = [
                {"index": ref["slot"] - 1, "imageFile": ref["comfy_path"], "imageB64": ""}
                for ref in segment_refs
            ] + [
                {"index": 5 + index, "imageFile": ref["comfy_path"], "imageB64": ""}
                for index, ref in enumerate(references)
            ]
        else:
            refs_for_segment = [
                {"index": index, "imageFile": ref["comfy_path"], "imageB64": ""}
                for index, ref in enumerate(references)
            ]
        timeline_segments.append(
            {
                "id": f"web_segment_{seg['id']}",
                "start": start,
                "length": length,
                "frameCount": length,
                "durationSec": seg["duration"],
                "prompt": segment_prompt(seg, len(references), segment_by_id.get(seg.get("continuity_source_id"))),
                "negativePrompt": project.get("continuity_negative_prompt", "").strip(),
                "taskType": "",
                "refs": refs_for_segment,
                "refAudios": [],
                "refVideos": [],
                "genImage": {"imageFile": "", "fileName": ""},
                "continuityFromPrev": seg["continuity_mode"] == "continue" and seg["position"] > 0,
                "continuitySourceIndex": index_by_id.get(seg.get("continuity_source_id")),
            }
        )
        start += length
    global_text = common_prompt(project, len(references))
    return {
        "version": 5,
        "editMode": "segment",
        "timelineMode": "prompt_batch",
        "totalFrames": total,
        "frameRate": project["fps"],
        "width": project["width"],
        "height": project["height"],
        "refMaxSize": max(project["width"], project["height"]),
        "video": {"fileName": "", "videoFile": "", "subfolder": "", "type": "input", "frames": [], "frameMap": []},
        "videoClips": [],
        "global": {
            "taskType": TASK_TYPE,
            "prompt": global_text,
            "refs": refs,
            "refAudios": [],
            "refVideos": [],
            "referenceVideo": {},
            "commonEnabled": True,
            "commonCollapsed": False,
            "continuousReference": False,
            "genImage": {"imageFile": ""},
        },
        "output": {
            "mode": "fixed",
            "width": project["width"],
            "height": project["height"],
            "exportMode": "segments",
            "audioMode": "generate",
            "continuityEnabled": True,
            "continuityOverlapFrames": 22,
        },
        "segments": timeline_segments,
        "runSelectEnabled": True,
        "runSelection": [selected_position],
        "liveTaePreview": False,
        "gen": {"defaultFrameCount": 481},
    }


def build_prompt(project, references, segments, selected_position, job_id):
    timeline = build_timeline(project, references, segments, selected_position)
    total = timeline["totalFrames"]
    director_id = str(100000 + int(project["id"]))
    selected_segment = segments[selected_position]
    prefix = f"webapp_jobs/{job_id}/segment"
    prompt = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        director_id: {
            "class_type": "MiniMaxH3Director",
            "inputs": {
                "model": ["1", 0], "video_vae": ["3", 0], "audio_vae": ["4", 0], "clip": ["2", 0],
                "task_type": TASK_TYPE, "global_prompt": timeline["global"]["prompt"],
                "bd_grp_sample": "采样设置", "cfg": 1.0, "seed": int(project["seed"]),
                "frame_rate": float(project["fps"]), "width": project["width"], "height": project["height"],
                "ref_max_size": max(project["width"], project["height"]), "total_frames": total,
                "timeline_data": json.dumps(timeline, ensure_ascii=False, separators=(",", ":")),
                "bd_grp_advanced": "高级采样", "steps": project["steps"], "sampler": "res_multistep",
                "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0,
                "bd_grp_perf": "性能", "clear_vram_between_segments": True, "export_source_images": False,
            },
        },
        "900001": {"class_type": "CreateVideo", "inputs": {"images": [director_id, 0], "audio": [director_id, 1], "fps": [director_id, 2], "bit_depth": 8}},
        "900002": {"class_type": "SaveVideo", "inputs": {"video": ["900001", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"}},
    }
    if selected_segment.get("refine_enabled"):
        prompt["900000"] = {
            "class_type": "MiniMaxH3DirectorRefine",
            "inputs": {
                "mode": "refine",
                "upscale_method": "lanczos",
                "denoise": float(selected_segment.get("refine_denoise") or 0.25),
                "steps": int(selected_segment.get("refine_steps") or 0),
                "seed_mode": "inherit",
                "aspect_ratio": "跟随导演台",
                "megapixels": 1.0,
                "width": project["width"],
                "height": project["height"],
                "skip_fl2v": True,
            },
        }
        prompt[director_id]["inputs"]["refine"] = ["900000", 0]
    return prompt, director_id


def submit_and_wait(prompt, director_id, progress_callback):
    client_id = str(uuid.uuid4())
    with ws_connect(f"{COMFY_WS}?clientId={client_id}", open_timeout=10, close_timeout=5, max_size=WEBSOCKET_MAX_SIZE) as ws:
        response = httpx.post(f"{COMFY_HTTP}/prompt", json={"prompt": prompt, "client_id": client_id}, timeout=30)
        if response.is_error:
            raise RuntimeError(f"ComfyUI 拒绝任务：{response.text}")
        prompt_id = response.json()["prompt_id"]
        progress_callback(prompt_id=prompt_id, status="running", progress=0.01, phase="任务已提交")
        while True:
            message = ws.recv(timeout=3600)
            if isinstance(message, bytes):
                continue
            event = json.loads(message)
            event_type = event.get("type")
            data = event.get("data") or {}
            if event_type == "minimax_director_progress" and str(data.get("node_id")) == director_id:
                maximum = float(data.get("overall_max") or 1)
                value = float(data.get("overall_value") or 0)
                progress_callback(progress=min(0.96, max(0.02, value / maximum)), phase=data.get("phase_label") or data.get("phase") or "生成中")
            elif event_type == "progress" and str(data.get("node")) == director_id:
                maximum = float(data.get("max") or 1)
                value = float(data.get("value") or 0)
                progress_callback(progress=min(0.94, max(0.02, value / maximum)), phase="模型采样")
            elif event_type == "execution_error" and data.get("prompt_id") == prompt_id:
                raise RuntimeError(data.get("exception_message") or data.get("exception_type") or "ComfyUI 生成失败")
            elif event_type == "executing" and data.get("prompt_id") == prompt_id and data.get("node") is None:
                break
    history = None
    for _ in range(20):
        response = httpx.get(f"{COMFY_HTTP}/history/{prompt_id}", timeout=15)
        response.raise_for_status()
        history = response.json().get(prompt_id)
        if history:
            break
        time.sleep(0.5)
    if not history:
        raise RuntimeError("任务完成，但无法读取 ComfyUI 历史记录")
    return prompt_id, history


def find_saved_video(history):
    output = (history.get("outputs") or {}).get("900002") or {}
    candidates = output.get("videos") or output.get("video") or output.get("images") or output.get("gifs") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    for item in candidates:
        filename = item.get("filename")
        if not filename:
            continue
        subfolder = item.get("subfolder") or ""
        path = (COMFY_OUTPUT_DIR / subfolder / filename).resolve()
        if path.is_file() and COMFY_OUTPUT_DIR.resolve() in path.parents:
            return path
    raise RuntimeError("ComfyUI 已完成，但没有找到保存的视频文件")


def archive_video(source, project_id, segment, job_id):
    folder = VIDEO_DIR / f"project_{project_id}" / f"segment_{segment['position'] + 1:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{job_id}.mp4"
    destination = folder / filename
    shutil.copy2(source, destination)
    return destination, destination.relative_to(VIDEO_DIR).as_posix()
