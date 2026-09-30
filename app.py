import base64
import hashlib
import hmac
import json
import mimetypes
import queue
import re
import shutil
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from comfy_client import TASK_TYPE, archive_video, build_prompt, find_saved_video, frame_count, submit_and_wait
from config import COMFY_HTTP, COMFY_INPUT_DIR, COMFY_OUTPUT_DIR, STATIC_DIR, UPLOAD_DIR, VIDEO_DIR
from database import connect, create_password, init_db, now_iso, rows
from video_media import analyze_video, concat_videos, remove_video_assets


app = FastAPI(title="SceneFlow H3", version="1.0.0")
job_queue = queue.Queue()
worker_started = False
ALLOWED_DURATIONS = (2, 3, 5, 6, 8, 10, 12, 15, 18, 20)
external_running_since = {}
SESSION_COOKIE = "sceneflow_session"
SESSION_DAYS = 30


class JobCancelled(Exception):
    pass


def request_user(request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def resource_owner(db, path):
    patterns = (
        (r"^/api/projects/(\d+)", "SELECT user_id FROM projects WHERE id=?"),
        (r"^/api/references/(\d+)/", "SELECT user_id FROM projects WHERE id=?"),
        (r"^/api/segments/(\d+)", "SELECT p.user_id FROM segments s JOIN projects p ON p.id=s.project_id WHERE s.id=?"),
        (r"^/api/jobs/([^/]+)", "SELECT p.user_id FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.id=?"),
        (r"^/api/videos/(\d+)", "SELECT p.user_id FROM videos v JOIN projects p ON p.id=v.project_id WHERE v.id=?"),
        (r"^/api/combined-videos/([^/]+)", "SELECT p.user_id FROM combined_videos v JOIN projects p ON p.id=v.project_id WHERE v.id=?"),
    )
    for pattern, query in patterns:
        match = re.match(pattern, path)
        if match:
            row = db.execute(query, (match.group(1),)).fetchone()
            return row[0] if row else None
    return False


@app.middleware("http")
async def authenticate_request(request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/auth/login":
        token = request.cookies.get(SESSION_COOKIE, "")
        token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
        with connect() as db:
            row = db.execute(
                """
                SELECT u.id,u.username,u.is_admin,u.is_active,s.expires_at
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=?
                """,
                (token_hash,),
            ).fetchone()
            if not row or not row["is_active"] or row["expires_at"] <= now_iso():
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            request.state.user = dict(row)
            if path.startswith("/api/admin/") or path.startswith("/api/settings/llm"):
                if not row["is_admin"]:
                    return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
            owner = resource_owner(db, path)
            if owner is None:
                return JSONResponse({"detail": "资源不存在"}, status_code=404)
            if owner is not False and owner != row["id"]:
                return JSONResponse({"detail": "无权访问此资源"}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    character_prompt: str = "固定穿着和外貌在所有片段中保持一致。"
    environment_prompt: str = "同一时间、天气和地点，空间布局与光线方向连续一致。"
    continuity_negative_prompt: str = "禁止换脸、人物身份漂移、服装突变、场景跳变、光线突变、动作断裂、重复动作和镜头轴线无故改变。"
    visual_prompt: str = "电影级写实风格，真实人体比例，稳定流畅运镜，自然光影。"
    audio_prompt: str = "环境声自然连续，背景音乐低于对白和环境声。"
    width: int = 608
    height: int = 352
    steps: int = 16
    seed: int = 42


class ProjectUpdate(ProjectCreate):
    pass


class SegmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    duration: int = 20
    continuity_mode: str = "continue"
    story_direction: str = ""
    world_prompt: str = ""
    prompt: str = Field(min_length=1)
    ending_prompt: str = ""
    refine_enabled: bool = False
    refine_denoise: float = Field(default=0.25, ge=0.05, le=0.85)
    refine_steps: int = Field(default=10, ge=0, le=50)
    continuity_source_id: int | None = None
    chain_order: int | None = Field(default=None, ge=1)


class SegmentUpdate(SegmentCreate):
    pass


class StoryboardGenerate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    duration: int
    continuity_mode: str = "continue"
    continuity_source_id: int | None = None
    story_direction: str = Field(default="", max_length=10000)
    world_prompt: str = Field(default="", max_length=10000)
    current_prompt: str = Field(default="", max_length=30000)


class ChainOrderUpdate(BaseModel):
    segment_ids: list[int]


class VideoSelectionUpdate(BaseModel):
    video_id: int


class MergeInclusionUpdate(BaseModel):
    include_in_merge: bool


class LLMConfigUpdate(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=1000)
    model_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=200)


class UserPasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=200)


class UserStatusUpdate(BaseModel):
    is_active: bool


def get_project(db, project_id):
    row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    return dict(row)


def get_segment(db, segment_id):
    row = db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "片段不存在")
    return dict(row)


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


def masked_api_key(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 12}{value[-4:]}"


def llm_stream_text(response):
    parts = []
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        content = (choices[0].get("delta") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts).strip()


def validate_canvas(width, height):
    if width < 32 or height < 32 or width % 32 or height % 32:
        raise HTTPException(422, "宽高必须是32的倍数")
    if width * height > 608 * 352:
        raise HTTPException(422, "当前显存配置限制最大画布为608×352")
    if max(width, height) > 672:
        raise HTTPException(422, "当前显存配置限制画布最长边不超过672像素")


def validate_segment_source(db, project_id, segment_id, source_id, mode):
    if mode != "continue":
        return None
    if source_id is None:
        raise HTTPException(422, "接续镜头必须指定来源片段")
    if segment_id is not None and int(source_id) == int(segment_id):
        raise HTTPException(422, "片段不能接续自身")
    source = db.execute(
        "SELECT id FROM segments WHERE id=? AND project_id=?",
        (source_id, project_id),
    ).fetchone()
    if not source:
        raise HTTPException(422, "接续来源片段不存在")
    return int(source_id)


def project_payload(db, project_id):
    project = get_project(db, project_id)
    project["references"] = rows(db.execute("SELECT * FROM character_references WHERE project_id = ? ORDER BY slot", (project_id,)).fetchall())
    segments = rows(db.execute("SELECT * FROM segments WHERE project_id = ? ORDER BY COALESCE(chain_order, position + 1), position", (project_id,)).fetchall())
    last_position = max((segment["position"] for segment in segments), default=-1)
    for display_order, segment in enumerate(segments, 1):
        segment["timeline_position"] = segment["position"]
        segment["position"] = display_order - 1
        segment["display_order"] = display_order
        segment["can_delete"] = segment["timeline_position"] == last_position
        segment["references"] = rows(db.execute("SELECT * FROM segment_references WHERE segment_id=? ORDER BY slot", (segment["id"],)).fetchall())
        segment["videos"] = rows(db.execute(
            """
            SELECT v.*
            FROM videos v
            LEFT JOIN jobs j ON j.id = v.job_id
            WHERE v.segment_id = ?
            ORDER BY COALESCE(j.created_at, v.created_at) DESC, v.id DESC
            """,
            (segment["id"],),
        ).fetchall())
        for index, video in enumerate(segment["videos"]):
            video["is_latest"] = index == 0
            video["selected_for_merge"] = video["id"] == segment.get("selected_video_id")
            video["cache_version"] = video.get("job_id") or f"{video['id']}-{video['created_at']}"
            try:
                video["keyframes"] = json.loads(video.get("keyframes_json") or "[]")
            except json.JSONDecodeError:
                video["keyframes"] = []
        segment["latest_job"] = db.execute("SELECT * FROM jobs WHERE segment_id = ? ORDER BY created_at DESC LIMIT 1", (segment["id"],)).fetchone()
        if segment["latest_job"]:
            segment["latest_job"] = dict(segment["latest_job"])
        segment["children"] = []
    by_id = {segment["id"]: segment for segment in segments}
    for segment in segments:
        source = by_id.get(segment.get("continuity_source_id"))
        if source:
            source["children"].append(segment["id"])
    project["segments"] = segments
    project["combined_videos"] = rows(db.execute("SELECT * FROM combined_videos WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall())
    for video in project["combined_videos"]:
        video["source_video_ids"] = json.loads(video["source_video_ids_json"])
    return project


def project_export(db, project_id):
    project = get_project(db, project_id)
    segments = rows(db.execute("SELECT * FROM segments WHERE project_id=? ORDER BY COALESCE(chain_order,position + 1),position", (project_id,)).fetchall())
    order_by_id = {segment["id"]: index for index, segment in enumerate(segments, 1)}
    storyboard = []
    for order, segment in enumerate(segments, 1):
        source_order = order_by_id.get(segment.get("continuity_source_id"))
        item = {
            "order": order,
            "title": segment["title"],
            "duration_seconds": segment["duration"],
            "continuity": {
                "mode": segment["continuity_mode"],
                "source_order": source_order,
            },
            "storyboard": segment["prompt"],
            "story_direction": segment.get("story_direction", ""),
            "world_setting": segment.get("world_prompt", ""),
            "ending_continuity_state": segment["ending_prompt"],
        }
        item["sampling"] = {
            "frame_count": segment["frame_count"],
            "refine_enabled": bool(segment["refine_enabled"]),
            "refine_denoise": segment["refine_denoise"],
            "refine_steps": segment["refine_steps"],
        }
        item["generation_history"] = rows(db.execute(
            "SELECT id,prompt_id,status,progress,phase,error,output_path,created_at,started_at,finished_at FROM jobs WHERE segment_id=? ORDER BY created_at",
            (segment["id"],),
        ).fetchall())
        item["videos"] = rows(db.execute(
            "SELECT id,job_id,filename,relative_path,size_bytes,duration_seconds,refined,width,height,video_frame_count,created_at FROM videos WHERE segment_id=? ORDER BY created_at,id",
            (segment["id"],),
        ).fetchall())
        storyboard.append(item)
    exported_at = now_iso()
    merged_storyboard = "\n\n".join(
        f"## {item['order']:02d} {item['title']}（{item['duration_seconds']}秒）\n"
        f"衔接：{'接续分镜 ' + str(item['continuity']['source_order']) if item['continuity']['source_order'] else '新镜头'}\n\n"
        f"{item['storyboard']}"
        + (f"\n\n结尾续拍状态：\n{item['ending_continuity_state']}" if item["ending_continuity_state"] else "")
        for item in storyboard
    )
    export = {
        "schema": "sceneflow-project-export",
        "schema_version": 1,
        "export_type": "full_project",
        "exported_at": exported_at,
        "project": {"name": project["name"]},
        "storyboard": storyboard,
        "merged_storyboard": merged_storyboard,
    }
    references = []
    for reference in rows(db.execute("SELECT * FROM character_references WHERE project_id=? ORDER BY slot", (project_id,)).fetchall()):
        path = (UPLOAD_DIR / reference["filename"]).resolve()
        content = path.read_bytes() if path.is_file() and UPLOAD_DIR.resolve() in path.parents else b""
        references.append({
            "slot": reference["slot"],
            "filename": reference["filename"],
            "media_type": mimetypes.guess_type(reference["filename"])[0] or "application/octet-stream",
            "sha256": hashlib.sha256(content).hexdigest() if content else None,
            "data_base64": base64.b64encode(content).decode("ascii") if content else None,
        })
    export["project"].update({
        "character_prompt": project["character_prompt"],
        "environment_prompt": project["environment_prompt"],
        "continuity_negative_prompt": project["continuity_negative_prompt"],
        "visual_prompt": project["visual_prompt"],
        "audio_prompt": project["audio_prompt"],
        "sampling": {
            "width": project["width"],
            "height": project["height"],
            "fps": project["fps"],
            "steps": project["steps"],
            "seed": project["seed"],
        },
        "generation_profile": {
            "task_type": TASK_TYPE,
            "models": {
                "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "video_vae": "minimax_h3_video_vae_fp16.safetensors",
                "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
            },
            "sampler": "res_multistep",
            "scheduler": "simple",
            "cfg": 1.0,
            "shift_video": 12.0,
            "shift_audio": 3.0,
            "continuity_overlap_frames": 22,
            "audio_mode": "generate",
        },
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "character_references": references,
    })
    export["combined_videos"] = rows(db.execute(
        "SELECT id,filename,relative_path,source_video_ids_json,size_bytes,duration_seconds,width,height,video_frame_count,created_at FROM combined_videos WHERE project_id=? ORDER BY created_at",
        (project_id,),
    ).fetchall())
    return export


def update_job(job_id, **fields):
    if not fields:
        return
    with connect() as db:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))


def job_is_cancelled(job_id):
    with connect() as db:
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row["status"] == "cancelled")


def job_context(job_id):
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return None
        job = dict(job)
        return job, get_project(db, job["project_id"]), get_segment(db, job["segment_id"])


def finalize_job(job_id, history):
    if job_is_cancelled(job_id):
        return
    context = job_context(job_id)
    if not context:
        return
    job, project, segment = context
    with connect() as db:
        existing = db.execute("SELECT 1 FROM videos WHERE job_id=?", (job_id,)).fetchone()
    if existing:
        update_job(job_id, status="complete", progress=1.0, phase="已完成", error=None, finished_at=job.get("finished_at") or now_iso())
        with connect() as db:
            db.execute("UPDATE segments SET status='complete',updated_at=? WHERE id=?", (now_iso(), segment["id"]))
        return
    update_job(job_id, progress=0.97, phase="归档视频", error=None)
    source = find_saved_video(history)
    destination, relative = archive_video(source, project["id"], segment, job_id)
    media = analyze_video(destination, relative)
    with connect() as db:
        db.execute(
            "INSERT INTO videos(project_id,segment_id,job_id,filename,relative_path,size_bytes,duration_seconds,refined,width,height,video_frame_count,keyframes_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project["id"], segment["id"], job_id, destination.name, relative, destination.stat().st_size, media["duration_seconds"], int(segment.get("refine_enabled") or 0), media["width"], media["height"], media["video_frame_count"], json.dumps(media["keyframes"], ensure_ascii=False), now_iso()),
        )
        video_id = db.execute("SELECT id FROM videos WHERE job_id=?", (job_id,)).fetchone()[0]
        db.execute("UPDATE segments SET selected_video_id=? WHERE id=?", (video_id, segment["id"]))
        db.execute("UPDATE segments SET status='complete',updated_at=? WHERE id=?", (now_iso(), segment["id"]))
    update_job(job_id, status="complete", progress=1.0, phase="已完成", error=None, output_path=relative, finished_at=now_iso())


def fail_job(job_id, message):
    update_job(job_id, status="failed", phase="生成失败", error=str(message), finished_at=now_iso())
    with connect() as db:
        db.execute("UPDATE segments SET status='failed',updated_at=? WHERE id=(SELECT segment_id FROM jobs WHERE id=?)", (now_iso(), job_id))


def run_job(job_id):
    job = None
    try:
        with connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND status='queued'", (job_id,)).fetchone()
            if not job:
                return False
            job = dict(job)
            segment = get_segment(db, job["segment_id"])
            if segment["continuity_mode"] == "continue" and segment.get("continuity_source_id"):
                source_active = db.execute(
                    "SELECT 1 FROM jobs WHERE segment_id=? AND status IN ('queued','running') LIMIT 1",
                    (segment["continuity_source_id"],),
                ).fetchone()
                if source_active:
                    db.execute("UPDATE jobs SET phase='等待来源片段完成',error=NULL WHERE id=? AND status='queued'", (job_id,))
                    return True
                has_video = db.execute("SELECT 1 FROM videos WHERE segment_id=? LIMIT 1", (segment["continuity_source_id"],)).fetchone()
                if not has_video:
                    raise RuntimeError("接续生成失败：来源片段没有可用成片")
            claimed = db.execute(
                "UPDATE jobs SET status='running',progress=0.01,phase='连接 ComfyUI',started_at=? WHERE id=? AND status='queued'",
                (now_iso(), job_id),
            )
            if not claimed.rowcount:
                return False
            project = get_project(db, job["project_id"])
            references = rows(db.execute("SELECT * FROM character_references WHERE project_id = ? ORDER BY slot", (project["id"],)).fetchall())
            segments = rows(db.execute("SELECT * FROM segments WHERE project_id = ? ORDER BY position", (project["id"],)).fetchall())
            for item in segments:
                item["references"] = rows(db.execute("SELECT * FROM segment_references WHERE segment_id=? ORDER BY slot", (item["id"],)).fetchall())
        if not references:
            raise RuntimeError("请先上传至少一张人物参考照片")
        prompt, director_id = build_prompt(project, references, segments, segment["position"], job_id)
        with connect() as db:
            db.execute("UPDATE segments SET status='running',updated_at=? WHERE id=?", (now_iso(), segment["id"]))

        def progress_callback(**fields):
            if fields.get("prompt_id"):
                update_job(job_id, prompt_id=fields["prompt_id"])
            if job_is_cancelled(job_id):
                if fields.get("prompt_id"):
                    try:
                        httpx.post(f"{COMFY_HTTP}/interrupt", json={"prompt_id": fields["prompt_id"]}, timeout=10).raise_for_status()
                    except httpx.HTTPError:
                        pass
                raise JobCancelled()
            update_job(job_id, **fields)

        prompt_id, history = submit_and_wait(prompt, director_id, progress_callback)
        if job_is_cancelled(job_id):
            return False
        update_job(job_id, prompt_id=prompt_id)
        finalize_job(job_id, history)
        return False
    except JobCancelled:
        return False
    except Exception as exc:
        if job_is_cancelled(job_id):
            return False
        with connect() as db:
            interrupted = db.execute("SELECT prompt_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if interrupted and interrupted["prompt_id"]:
            prompt_id = interrupted["prompt_id"]
            try:
                history = comfy_history(prompt_id)
                if history:
                    status = history.get("status") or {}
                    if status.get("status_str") == "error" or status.get("completed") is False:
                        fail_job(job_id, "ComfyUI任务执行失败")
                    else:
                        finalize_job(job_id, history)
                    return False
                running, pending = comfy_queue_prompt_ids()
                if job and (prompt_id in running or prompt_id in pending):
                    update_job(job_id, status="running" if prompt_id in running else "queued", phase="连接中断，已切换恢复监控", error=None, finished_at=None)
                    director_id = str(100000 + int(job["project_id"]))
                    monitor_recovered_job(job_id, prompt_id, director_id)
                    return False
            except Exception:
                pass
        fail_job(job_id, exc)
        return False


def worker_loop():
    while True:
        job_id = job_queue.get()
        try:
            waiting_for_source = run_job(job_id)
            if waiting_for_source:
                job_queue.put(job_id)
                threading.Event().wait(1)
        finally:
            job_queue.task_done()


def backfill_video_metadata():
    with connect() as db:
        pending = rows(
            db.execute(
                """
                SELECT v.*, s.refine_enabled
                FROM videos v JOIN segments s ON s.id=v.segment_id
                WHERE v.duration_seconds IS NULL OR v.keyframes_json='[]'
                """
            ).fetchall()
        )
    for video in pending:
        path = (VIDEO_DIR / video["relative_path"]).resolve()
        if not path.is_file() or VIDEO_DIR.resolve() not in path.parents:
            continue
        try:
            media = analyze_video(path, video["relative_path"])
            with connect() as db:
                db.execute(
                    "UPDATE videos SET duration_seconds=?,refined=?,width=?,height=?,video_frame_count=?,keyframes_json=? WHERE id=?",
                    (media["duration_seconds"], int(video.get("refine_enabled") or 0), media["width"], media["height"], media["video_frame_count"], json.dumps(media["keyframes"], ensure_ascii=False), video["id"]),
                )
        except Exception:
            continue


def comfy_queue_prompt_ids():
    response = httpx.get(f"{COMFY_HTTP}/queue", timeout=5)
    response.raise_for_status()
    payload = response.json()
    running = {str(item[1]) for item in payload.get("queue_running") or [] if len(item) > 1}
    pending = {str(item[1]) for item in payload.get("queue_pending") or [] if len(item) > 1}
    return running, pending


def comfy_history(prompt_id):
    response = httpx.get(f"{COMFY_HTTP}/history/{prompt_id}", timeout=10)
    response.raise_for_status()
    return response.json().get(prompt_id)


def monitor_recovered_job(job_id, prompt_id, director_id):
    while True:
        if job_is_cancelled(job_id):
            return
        try:
            history = comfy_history(prompt_id)
            if history:
                status = history.get("status") or {}
                if status.get("status_str") == "error" or status.get("completed") is False:
                    fail_job(job_id, "ComfyUI任务执行失败")
                else:
                    finalize_job(job_id, history)
                return
            running, pending = comfy_queue_prompt_ids()
            if prompt_id in running:
                try:
                    progress_response = httpx.get(f"{COMFY_HTTP}/minimax/director/progress", params={"prompt_id": prompt_id}, timeout=3)
                    snapshot = progress_response.json() if progress_response.is_success else {}
                except Exception:
                    snapshot = {}
                fields = {"status": "running", "phase": snapshot.get("phase_label") or "恢复监控：ComfyUI正在执行", "error": None, "finished_at": None}
                if snapshot:
                    maximum = float(snapshot.get("overall_max") or 1)
                    fields["progress"] = min(0.96, max(0.01, float(snapshot.get("overall_value") or 0) / maximum))
                update_job(job_id, **fields)
                with connect() as db:
                    db.execute("UPDATE segments SET status='running',updated_at=? WHERE id=(SELECT segment_id FROM jobs WHERE id=?)", (now_iso(), job_id))
            elif prompt_id in pending:
                update_job(job_id, status="queued", phase="恢复监控：ComfyUI队列等待", error=None, finished_at=None)
                with connect() as db:
                    db.execute("UPDATE segments SET status='queued',updated_at=? WHERE id=(SELECT segment_id FROM jobs WHERE id=?)", (now_iso(), job_id))
            else:
                # History publication can lag briefly after leaving the queue.
                update_job(job_id, phase="恢复监控：等待ComfyUI结果")
            threading.Event().wait(2)
        except Exception:
            update_job(job_id, phase="恢复监控：等待ComfyUI重新连接", error=None, finished_at=None)
            threading.Event().wait(5)


def recover_jobs():
    try:
        running, pending = comfy_queue_prompt_ids()
    except Exception:
        running, pending = set(), set()
    with connect() as db:
        candidates = rows(db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') OR (prompt_id IS NOT NULL AND output_path IS NOT NULL) ORDER BY created_at").fetchall())
    for job in candidates:
        prompt_id = job.get("prompt_id")
        if not prompt_id:
            if job["status"] in {"queued", "running"}:
                update_job(job["id"], status="queued", phase="服务恢复：重新进入Web队列", error=None, finished_at=None)
                job_queue.put(job["id"])
            continue
        history = None
        try:
            history = comfy_history(prompt_id)
        except Exception:
            pass
        if history:
            threading.Thread(target=lambda jid=job["id"], h=history: finalize_job(jid, h), daemon=True, name=f"recover-finalize-{job['id'][:8]}").start()
        elif prompt_id in running or prompt_id in pending:
            update_job(job["id"], status="running" if prompt_id in running else "queued", phase="服务恢复：已重新识别ComfyUI任务", error=None, finished_at=None)
            with connect() as db:
                db.execute("UPDATE segments SET status=?,updated_at=? WHERE id=?", ("running" if prompt_id in running else "queued", now_iso(), job["segment_id"]))
            director_id = str(100000 + int(job["project_id"]))
            threading.Thread(target=monitor_recovered_job, args=(job["id"], prompt_id, director_id), daemon=True, name=f"recover-monitor-{job['id'][:8]}").start()
        elif job["status"] in {"queued", "running"}:
            fail_job(job["id"], "Web服务恢复时未在ComfyUI队列或历史中找到该任务")


@app.on_event("startup")
def startup():
    global worker_started
    init_db()
    if not worker_started:
        threading.Thread(target=worker_loop, daemon=True, name="generation-worker").start()
        threading.Thread(target=backfill_video_metadata, daemon=True, name="video-metadata-backfill").start()
        threading.Thread(target=recover_jobs, daemon=True, name="job-recovery").start()
        worker_started = True


@app.post("/api/auth/login")
def login(data: LoginRequest):
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (data.username.strip(),)).fetchone()
        if not user or not user["is_active"] or not hmac.compare_digest(password_hash(data.password, user["password_salt"]), user["password_hash"]):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(48)
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user["id"], (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(), now_iso()),
        )
    response = JSONResponse({"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict", secure=False, path="/")
    return response


@app.get("/api/auth/me")
def current_user(request: Request):
    user = request_user(request)
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/admin/users")
def list_users(request: Request):
    request_user(request)
    with connect() as db:
        return rows(db.execute(
            """
            SELECT u.id,u.username,u.is_admin,u.is_active,u.created_at,u.updated_at,
                   (SELECT COUNT(*) FROM projects p WHERE p.user_id=u.id) project_count
            FROM users u ORDER BY u.is_admin DESC,u.username COLLATE NOCASE
            """
        ).fetchall())


@app.post("/api/admin/users")
def create_user(data: UserCreate, request: Request):
    request_user(request)
    salt, digest = create_password(data.password)
    stamp = now_iso()
    try:
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO users(username,password_salt,password_hash,is_admin,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (data.username.strip(), salt, digest, 0, 1, stamp, stamp),
            )
            return {"id": cursor.lastrowid, "username": data.username.strip(), "is_admin": False, "is_active": True}
    except Exception as error:
        if "UNIQUE constraint failed" in str(error):
            raise HTTPException(409, "用户名已存在")
        raise


@app.put("/api/admin/users/{user_id}/password")
def update_user_password(user_id: int, data: UserPasswordUpdate, request: Request):
    request_user(request)
    salt, digest = create_password(data.password)
    with connect() as db:
        updated = db.execute("UPDATE users SET password_salt=?,password_hash=?,updated_at=? WHERE id=?", (salt, digest, now_iso(), user_id))
        if not updated.rowcount:
            raise HTTPException(404, "用户不存在")
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/status")
def update_user_status(user_id: int, data: UserStatusUpdate, request: Request):
    current = request_user(request)
    with connect() as db:
        target = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")
        if target["is_admin"]:
            raise HTTPException(409, "管理员账号不能停用")
        db.execute("UPDATE users SET is_active=?,updated_at=? WHERE id=?", (int(data.is_active), now_iso(), user_id))
        if not data.is_active:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True, "is_active": data.is_active}


@app.get("/api/health")
def health():
    try:
        response = httpx.get(f"{COMFY_HTTP}/system_stats", timeout=3)
        comfy = response.is_success
    except Exception:
        comfy = False
    return {"ok": True, "comfyui": comfy, "queue_size": job_queue.qsize()}


@app.get("/api/settings/llm")
def read_llm_config():
    with connect() as db:
        row = db.execute("SELECT base_url,api_key,model_name,updated_at FROM llm_config WHERE id=1").fetchone()
    if not row:
        return {"configured": False, "base_url": "", "api_key_masked": "", "model_name": "", "updated_at": None}
    return {"configured": True, "base_url": row["base_url"], "api_key_masked": masked_api_key(row["api_key"]), "model_name": row["model_name"], "updated_at": row["updated_at"]}


@app.put("/api/settings/llm")
def update_llm_config(data: LLMConfigUpdate):
    parsed = urlparse(data.base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(422, "Base URL必须是有效的HTTP或HTTPS地址")
    with connect() as db:
        current = db.execute("SELECT * FROM llm_config WHERE id=1").fetchone()
        if not current or not hmac.compare_digest(password_hash(data.password, current["password_salt"]), current["password_hash"]):
            raise HTTPException(403, "配置密码错误")
        api_key = data.api_key.strip() or current["api_key"]
        if not api_key:
            raise HTTPException(422, "API Key不能为空")
        db.execute(
            "UPDATE llm_config SET base_url=?,api_key=?,model_name=?,updated_at=? WHERE id=1",
            (data.base_url.strip().rstrip("/") + "/", api_key, data.model_name.strip(), now_iso()),
        )
    return {"ok": True}


@app.post("/api/projects/{project_id}/storyboard/generate")
def generate_storyboard(project_id: int, data: StoryboardGenerate):
    if data.duration not in ALLOWED_DURATIONS or data.continuity_mode not in ("continue", "cut"):
        raise HTTPException(422, "片段参数无效")
    with connect() as db:
        project = get_project(db, project_id)
        source = None
        if data.continuity_mode == "continue":
            source_id = validate_segment_source(db, project_id, None, data.continuity_source_id, data.continuity_mode)
            source = get_segment(db, source_id)
        config = db.execute("SELECT base_url,api_key,model_name FROM llm_config WHERE id=1").fetchone()
    if not config or not config["api_key"].strip():
        raise HTTPException(409, "请先在右上角配置大模型")

    if source:
        continuation = (
            f"直接接续片段《{source['title']}》。\n"
            f"来源片段分镜：\n{source['prompt']}\n\n"
            f"来源片段结尾状态：\n{source['ending_prompt'] or '未单独填写，请从来源分镜末尾推断。'}"
        )
    else:
        continuation = "这是新镜头，不继承其他片段末帧，但人物、世界和整体风格必须保持一致。"
    direction = data.story_direction.strip() or "未指定人工故事走向，请根据标题和已有世界设定自然推进剧情。"
    draft = data.current_prompt.strip() or "无现有分镜草稿，请从零生成。"
    system_prompt = (
        "你是连续短片的专业分镜导演。请输出可直接交给文生视频模型的中文分镜正文，不要解释、致歉、使用Markdown标题或代码块。"
        "必须严格适配指定时长，以连续时间段组织内容，并描述人物动作、表情、机位、景别、运镜、环境变化、对白或声音。"
        "动作应能在真实时长内完成，避免拥挤、瞬移、重复上一段动作、字幕、水印和无理由转场。"
        "接续镜头必须从来源片段最后状态的下一瞬间开始，结尾保留清晰、自然、可续拍的状态。"
    )
    user_prompt = f"""项目：《{project['name']}》
固定人物：{project['character_prompt']}
固定环境：{project['environment_prompt']}
视觉风格：{project['visual_prompt']}
声音风格：{project['audio_prompt']}
连续性禁忌：{project.get('continuity_negative_prompt', '')}

新片段标题：《{data.title.strip()}》
片段时长：{data.duration}秒
本片段世界设定（与全局冲突时优先）：{data.world_prompt.strip() or '未单独指定'}
衔接要求：{continuation}

人工故事走向：
{direction}

现有分镜草稿：
{draft}

请生成完整的{data.duration}秒本段剧情。正文从“0秒”开始，以覆盖全部{data.duration}秒的时间段结束；最后一段要包含声音及可续拍的画面状态。"""
    try:
        response = httpx.post(
            urljoin(config["base_url"].rstrip("/") + "/", "v1/chat/completions"),
            headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
            json={
                "model": config["model_name"],
                "stream": True,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=httpx.Timeout(120, connect=10),
        )
        response.raise_for_status()
        storyboard = llm_stream_text(response)
    except httpx.TimeoutException:
        raise HTTPException(504, "大模型响应超时，请稍后重试")
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, f"大模型服务返回错误（HTTP {error.response.status_code}）")
    except (httpx.RequestError, json.JSONDecodeError):
        raise HTTPException(502, "无法连接大模型服务，请检查配置")
    if not storyboard:
        raise HTTPException(502, "大模型未返回有效分镜内容")
    return {"storyboard": storyboard, "model_name": config["model_name"]}


def comfy_task_summary(item, status, queue_position=0):
    try:
        number, prompt_id, prompt, extra, _outputs = item[:5]
    except (TypeError, ValueError):
        return None
    director_id = None
    director = None
    save = None
    for node_id, node in (prompt or {}).items():
        class_type = node.get("class_type")
        if class_type == "MiniMaxH3Director":
            director_id, director = str(node_id), node
        elif class_type == "SaveVideo":
            save = node
    director_inputs = (director or {}).get("inputs") or {}
    save_inputs = (save or {}).get("inputs") or {}
    workflow = ((extra or {}).get("extra_pnginfo") or {}).get("workflow") or {}
    workflow_title = ""
    for node in workflow.get("nodes") or []:
        if str(node.get("id")) == director_id:
            workflow_title = str(node.get("title") or "").strip()
            break
    output_name = str(save_inputs.get("filename_prefix") or "").strip().split("/")[-1]
    title = workflow_title or output_name or "ComfyUI 视频生成任务"
    total_frames = int(director_inputs.get("total_frames") or 0)
    fps = float(director_inputs.get("frame_rate") or 24)
    duration = round(total_frames / max(fps, 1), 1) if total_frames else 0
    task_type = str(director_inputs.get("task_type") or "")
    task_mode = task_type.split("—", 1)[0].strip() or "视频"
    created_at = ""
    create_time = (extra or {}).get("create_time")
    if create_time:
        try:
            created_at = datetime.fromtimestamp(float(create_time) / 1000, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return {
        "id": f"comfy-{prompt_id}",
        "prompt_id": prompt_id,
        "status": status,
        "progress": None,
        "phase": "ComfyUI 正在执行" if status == "running" else f"ComfyUI 队列等待，第 {queue_position} 位",
        "project_name": "ComfyUI 外部任务",
        "segment_title": title,
        "segment_position": max(0, int(number) - 1),
        "duration": duration,
        "duration_label": f"{duration:g}秒 · {total_frames}帧" if total_frames else task_mode,
        "queue_position": queue_position if status == "queued" else 0,
        "created_at": created_at,
        "source": "comfyui",
        "task_mode": task_mode,
        "output_name": output_name,
    }


def director_progress_snapshots():
    try:
        response = httpx.get(f"{COMFY_HTTP}/minimax/director/progress", timeout=3)
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return {
            str(item.get("prompt_id")): item
            for item in (response.json().get("tasks") or [])
            if item.get("prompt_id")
        }
    except Exception:
        return {}


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def add_task_timing(task, snapshot=None):
    snapshot = snapshot or {}
    now = datetime.now(timezone.utc).timestamp()
    if snapshot:
        maximum = float(snapshot.get("overall_max") or 1)
        task["progress"] = max(0.0, min(1.0, float(snapshot.get("overall_value") or 0) / maximum))
        task["phase"] = snapshot.get("phase_label") or snapshot.get("phase") or task.get("phase")
        task["progress_available"] = True
        started = float(snapshot.get("started_at") or 0) or None
        task["elapsed_basis"] = "director"
    else:
        task["progress_available"] = task.get("progress") is not None
        if task.get("source") == "comfyui" and task.get("status") == "running":
            started = external_running_since.setdefault(task["prompt_id"], now)
            task["elapsed_basis"] = "observed"
        else:
            started = parse_timestamp(task.get("started_at"))
            task["elapsed_basis"] = "job"
    created = parse_timestamp(task.get("created_at"))
    finished = parse_timestamp(task.get("finished_at"))
    if task.get("status") == "queued":
        task["waiting_seconds"] = max(0, int(now - created)) if created else 0
        task["elapsed_seconds"] = 0
    else:
        start_time = started or created
        end_time = finished or now
        task["waiting_seconds"] = 0
        task["elapsed_seconds"] = max(0, int(end_time - start_time)) if start_time else 0
    progress = task.get("progress")
    if task.get("status") == "running" and progress is not None and 0.02 <= progress < 1 and task["elapsed_seconds"] >= 5:
        remaining = int(task["elapsed_seconds"] * (1 - progress) / progress)
        task["remaining_seconds"] = remaining
        task["estimated_finish_at"] = datetime.fromtimestamp(now + remaining, timezone.utc).isoformat()
    else:
        task["remaining_seconds"] = None
        task["estimated_finish_at"] = None
    return task


def comfy_queue_status(known_prompt_ids=None, snapshots=None):
    known_prompt_ids = set(known_prompt_ids or [])
    snapshots = snapshots or {}
    try:
        response = httpx.get(f"{COMFY_HTTP}/queue", timeout=3)
        response.raise_for_status()
        payload = response.json()
        running_items = [item for item in (payload.get("queue_running") or []) if len(item) > 1 and item[1] not in known_prompt_ids]
        pending_items = [item for item in (payload.get("queue_pending") or []) if len(item) > 1 and item[1] not in known_prompt_ids]
        tasks = []
        for item in running_items:
            summary = comfy_task_summary(item, "running")
            if summary:
                tasks.append(add_task_timing(summary, snapshots.get(summary["prompt_id"])))
        for position, item in enumerate(pending_items, 1):
            summary = comfy_task_summary(item, "queued", position)
            if summary:
                tasks.append(add_task_timing(summary))
        return {
            "online": True,
            "running": len(running_items),
            "pending": len(pending_items),
            "tasks": tasks,
        }
    except Exception:
        return {"online": False, "running": 0, "pending": 0, "tasks": []}


@app.get("/api/tasks/status")
def task_status(request: Request):
    user = request_user(request)
    with connect() as db:
        active = rows(
            db.execute(
                """
                SELECT j.*, p.name AS project_name, s.title AS segment_title,
                       COALESCE(s.chain_order, s.position + 1) - 1 AS segment_position, s.duration
                FROM jobs j
                JOIN projects p ON p.id = j.project_id
                JOIN segments s ON s.id = j.segment_id
                WHERE j.status IN ('running', 'queued') AND p.user_id=?
                ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END, j.created_at
                """
            , (user["id"],)).fetchall()
        )
        recent = rows(
            db.execute(
                """
                SELECT j.*, p.name AS project_name, s.title AS segment_title,
                       COALESCE(s.chain_order, s.position + 1) - 1 AS segment_position, s.duration
                FROM jobs j
                JOIN projects p ON p.id = j.project_id
                JOIN segments s ON s.id = j.segment_id
                WHERE j.status IN ('complete', 'failed', 'cancelled') AND p.user_id=?
                ORDER BY COALESCE(j.finished_at, j.created_at) DESC
                LIMIT 12
                """
            , (user["id"],)).fetchall()
        )
        all_prompt_ids = {row[0] for row in db.execute("SELECT prompt_id FROM jobs WHERE status IN ('running','queued') AND prompt_id IS NOT NULL").fetchall()}
        global_web = db.execute("SELECT SUM(status='running'),SUM(status='queued') FROM jobs WHERE status IN ('running','queued')").fetchone()
    waiting_position = 0
    for item in active:
        if item["status"] == "queued":
            waiting_position += 1
            item["queue_position"] = waiting_position
        else:
            item["queue_position"] = 0
        add_task_timing(item)
    for item in recent:
        add_task_timing(item)
    user_web_running = sum(item["status"] == "running" for item in active)
    user_web_queued = sum(item["status"] == "queued" for item in active)
    web_running = int(global_web[0] or 0)
    web_queued = int(global_web[1] or 0)
    comfy = comfy_queue_status(all_prompt_ids, director_progress_snapshots())
    comfy["tasks"] = []
    busy = bool(web_running or web_queued or comfy["running"] or comfy["pending"])
    return {
        "state": "offline" if not comfy["online"] else ("busy" if busy else "idle"),
        "busy": busy,
        "total": {
            "running": web_running + comfy["running"],
            "queued": web_queued + comfy["pending"],
        },
        "web": {"running": web_running, "queued": web_queued},
        "mine": {"running": user_web_running, "queued": user_web_queued},
        "comfyui": comfy,
        "active": active,
        "recent": recent,
    }


@app.get("/api/projects")
def list_projects(request: Request):
    user = request_user(request)
    with connect() as db:
        return rows(db.execute("SELECT p.*, (SELECT COUNT(*) FROM segments s WHERE s.project_id=p.id) segment_count FROM projects p WHERE p.user_id=? ORDER BY updated_at DESC", (user["id"],)).fetchall())


@app.post("/api/projects")
def create_project(data: ProjectCreate, request: Request):
    user = request_user(request)
    validate_canvas(data.width, data.height)
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO projects(user_id,name,character_prompt,environment_prompt,continuity_negative_prompt,visual_prompt,audio_prompt,width,height,fps,steps,seed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"], data.name.strip(), data.character_prompt, data.environment_prompt, data.continuity_negative_prompt, data.visual_prompt, data.audio_prompt, data.width, data.height, 24, data.steps, data.seed, stamp, stamp),
        )
        project_id = cursor.lastrowid
        return project_payload(db, project_id)


@app.get("/api/projects/{project_id}")
def read_project(project_id: int):
    with connect() as db:
        return project_payload(db, project_id)


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: int):
    with connect() as db:
        payload = project_export(db, project_id)
    filename = f"sceneflow_project_{project_id}_full.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/projects/{project_id}/export-storyboard")
def export_storyboard(project_id: int):
    with connect() as db:
        get_project(db, project_id)
        segments = rows(db.execute(
            "SELECT title,duration,prompt FROM segments WHERE project_id=? ORDER BY COALESCE(chain_order,position + 1),position",
            (project_id,),
        ).fetchall())
    content = "\n\n".join(
        f"[片段{index}]0-{segment['duration']}秒：\n"
        f"标题：{segment['title']}\n"
        f"分镜内容：\n{segment['prompt'].strip()}"
        for index, segment in enumerate(segments, 1)
    )
    filename = f"sceneflow_project_{project_id}_storyboard.txt"
    return Response(
        "\ufeff" + content + "\n",
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.put("/api/projects/{project_id}")
def update_project(project_id: int, data: ProjectUpdate):
    validate_canvas(data.width, data.height)
    with connect() as db:
        get_project(db, project_id)
        db.execute(
            "UPDATE projects SET name=?,character_prompt=?,environment_prompt=?,continuity_negative_prompt=?,visual_prompt=?,audio_prompt=?,width=?,height=?,steps=?,seed=?,updated_at=? WHERE id=?",
            (data.name.strip(), data.character_prompt, data.environment_prompt, data.continuity_negative_prompt, data.visual_prompt, data.audio_prompt, data.width, data.height, data.steps, data.seed, now_iso(), project_id),
        )
        return project_payload(db, project_id)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int):
    with connect() as db:
        get_project(db, project_id)
        running = db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()
        if running:
            raise HTTPException(409, "项目有生成任务，暂时不能删除")
        refs = rows(db.execute("SELECT * FROM character_references WHERE project_id=?", (project_id,)).fetchall())
        segment_refs = rows(db.execute("SELECT sr.* FROM segment_references sr JOIN segments s ON s.id=sr.segment_id WHERE s.project_id=?", (project_id,)).fetchall())
        videos = rows(db.execute("SELECT * FROM videos WHERE project_id=?", (project_id,)).fetchall())
        combined_videos = rows(db.execute("SELECT * FROM combined_videos WHERE project_id=?", (project_id,)).fetchall())
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    for item in refs:
        (UPLOAD_DIR / item["filename"]).unlink(missing_ok=True)
        (COMFY_INPUT_DIR.parent / item["comfy_path"]).unlink(missing_ok=True)
    for item in segment_refs:
        (UPLOAD_DIR / item["filename"]).unlink(missing_ok=True)
        (COMFY_INPUT_DIR.parent / item["comfy_path"]).unlink(missing_ok=True)
    for item in videos:
        try:
            keyframes = json.loads(item.get("keyframes_json") or "[]")
        except json.JSONDecodeError:
            keyframes = []
        remove_video_assets(item["relative_path"], keyframes)
    for item in combined_videos:
        path = (VIDEO_DIR / item["relative_path"]).resolve()
        if VIDEO_DIR.resolve() in path.parents:
            path.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/projects/{project_id}/references/{slot}")
async def upload_reference(project_id: int, slot: int, file: UploadFile = File(...)):
    if slot not in (1, 2):
        raise HTTPException(422, "人物图片槽位只能是1或2")
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if not content_type.startswith("image/"):
        raise HTTPException(422, "只能上传图片")
    data = await file.read()
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(422, "图片为空或超过15MB")
    suffix = Path(file.filename or "photo.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    safe_name = f"project_{project_id}_person_{slot}_{uuid.uuid4().hex[:10]}{suffix}"
    local_path = UPLOAD_DIR / safe_name
    comfy_path = COMFY_INPUT_DIR / safe_name
    local_path.write_bytes(data)
    shutil.copy2(local_path, comfy_path)
    comfy_relative = f"webapp/{safe_name}"
    with connect() as db:
        get_project(db, project_id)
        old = db.execute("SELECT * FROM character_references WHERE project_id=? AND slot=?", (project_id, slot)).fetchone()
        db.execute(
            "INSERT INTO character_references(project_id,slot,filename,comfy_path,created_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,slot) DO UPDATE SET filename=excluded.filename,comfy_path=excluded.comfy_path,created_at=excluded.created_at",
            (project_id, slot, safe_name, comfy_relative, now_iso()),
        )
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id))
    if old:
        (UPLOAD_DIR / old["filename"]).unlink(missing_ok=True)
        (COMFY_INPUT_DIR.parent / old["comfy_path"]).unlink(missing_ok=True)
    return {"slot": slot, "filename": safe_name, "url": f"/api/references/{project_id}/{slot}"}


@app.get("/api/references/{project_id}/{slot}")
def reference_image(project_id: int, slot: int):
    with connect() as db:
        row = db.execute("SELECT * FROM character_references WHERE project_id=? AND slot=?", (project_id, slot)).fetchone()
    if not row:
        raise HTTPException(404, "图片不存在")
    path = UPLOAD_DIR / row["filename"]
    return FileResponse(path)


@app.delete("/api/projects/{project_id}/references/{slot}")
def delete_reference(project_id: int, slot: int):
    with connect() as db:
        row = db.execute("SELECT * FROM character_references WHERE project_id=? AND slot=?", (project_id, slot)).fetchone()
        if not row:
            return {"ok": True}
        db.execute("DELETE FROM character_references WHERE id=?", (row["id"],))
    (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
    (COMFY_INPUT_DIR.parent / row["comfy_path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/segments/{segment_id}/references/{slot}")
async def upload_segment_reference(segment_id: int, slot: int, file: UploadFile = File(...)):
    if slot not in (1, 2, 3, 4, 5):
        raise HTTPException(422, "片段图片槽位只能是1至5")
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if not content_type.startswith("image/"):
        raise HTTPException(422, "只能上传图片")
    data = await file.read()
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(422, "图片为空或超过15MB")
    suffix = Path(file.filename or "photo.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    safe_name = f"segment_{segment_id}_constraint_{slot}_{uuid.uuid4().hex[:10]}{suffix}"
    local_path = UPLOAD_DIR / safe_name
    comfy_path = COMFY_INPUT_DIR / safe_name
    local_path.write_bytes(data)
    shutil.copy2(local_path, comfy_path)
    comfy_relative = f"webapp/{safe_name}"
    with connect() as db:
        segment = get_segment(db, segment_id)
        old = db.execute("SELECT * FROM segment_references WHERE segment_id=? AND slot=?", (segment_id, slot)).fetchone()
        db.execute(
            "INSERT INTO segment_references(segment_id,slot,filename,comfy_path,created_at) VALUES(?,?,?,?,?) ON CONFLICT(segment_id,slot) DO UPDATE SET filename=excluded.filename,comfy_path=excluded.comfy_path,created_at=excluded.created_at",
            (segment_id, slot, safe_name, comfy_relative, now_iso()),
        )
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), segment["project_id"]))
    if old:
        (UPLOAD_DIR / old["filename"]).unlink(missing_ok=True)
        (COMFY_INPUT_DIR.parent / old["comfy_path"]).unlink(missing_ok=True)
    return {"slot": slot, "filename": safe_name, "url": f"/api/segments/{segment_id}/references/{slot}"}


@app.get("/api/segments/{segment_id}/references/{slot}")
def segment_reference_image(segment_id: int, slot: int):
    with connect() as db:
        row = db.execute("SELECT * FROM segment_references WHERE segment_id=? AND slot=?", (segment_id, slot)).fetchone()
    if not row:
        raise HTTPException(404, "图片不存在")
    return FileResponse(UPLOAD_DIR / row["filename"], headers={"Cache-Control": "no-store"})


@app.delete("/api/segments/{segment_id}/references/{slot}")
def delete_segment_reference(segment_id: int, slot: int):
    with connect() as db:
        row = db.execute("SELECT * FROM segment_references WHERE segment_id=? AND slot=?", (segment_id, slot)).fetchone()
        if not row:
            return {"ok": True}
        db.execute("DELETE FROM segment_references WHERE id=?", (row["id"],))
    (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
    (COMFY_INPUT_DIR.parent / row["comfy_path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/projects/{project_id}/segments")
def create_segment(project_id: int, data: SegmentCreate):
    if data.duration not in ALLOWED_DURATIONS or data.continuity_mode not in ("continue", "cut"):
        raise HTTPException(422, "片段参数无效")
    stamp = now_iso()
    with connect() as db:
        get_project(db, project_id)
        position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM segments WHERE project_id=?", (project_id,)).fetchone()[0]
        mode = "cut" if position == 0 else data.continuity_mode
        source_id = validate_segment_source(db, project_id, None, data.continuity_source_id, mode)
        chain_order = data.chain_order or db.execute("SELECT COALESCE(MAX(chain_order), 0) + 1 FROM segments WHERE project_id=?", (project_id,)).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO segments(project_id,position,title,duration,frame_count,continuity_mode,story_direction,world_prompt,prompt,ending_prompt,refine_enabled,refine_denoise,refine_steps,continuity_source_id,chain_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, position, data.title.strip(), data.duration, frame_count(data.duration), mode, data.story_direction.strip(), data.world_prompt.strip(), data.prompt.strip(), data.ending_prompt.strip(), int(data.refine_enabled), data.refine_denoise, data.refine_steps, source_id, chain_order, stamp, stamp),
        )
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (stamp, project_id))
        return get_segment(db, cursor.lastrowid)


@app.put("/api/segments/{segment_id}")
def update_segment(segment_id: int, data: SegmentUpdate):
    if data.duration not in ALLOWED_DURATIONS or data.continuity_mode not in ("continue", "cut"):
        raise HTTPException(422, "片段参数无效")
    with connect() as db:
        segment = get_segment(db, segment_id)
        running = db.execute("SELECT 1 FROM jobs WHERE segment_id=? AND status IN ('queued','running')", (segment_id,)).fetchone()
        if running:
            raise HTTPException(409, "片段正在生成，不能修改")
        mode = "cut" if segment["position"] == 0 else data.continuity_mode
        source_id = validate_segment_source(db, segment["project_id"], segment_id, data.continuity_source_id, mode)
        chain_order = data.chain_order or segment.get("chain_order") or segment["position"] + 1
        db.execute(
            "UPDATE segments SET title=?,duration=?,frame_count=?,continuity_mode=?,story_direction=?,world_prompt=?,prompt=?,ending_prompt=?,refine_enabled=?,refine_denoise=?,refine_steps=?,continuity_source_id=?,chain_order=?,status='draft',updated_at=? WHERE id=?",
            (data.title.strip(), data.duration, frame_count(data.duration), mode, data.story_direction.strip(), data.world_prompt.strip(), data.prompt.strip(), data.ending_prompt.strip(), int(data.refine_enabled), data.refine_denoise, data.refine_steps, source_id, chain_order, now_iso(), segment_id),
        )
        return get_segment(db, segment_id)


@app.delete("/api/segments/{segment_id}")
def delete_segment(segment_id: int):
    with connect() as db:
        segment = get_segment(db, segment_id)
        last = db.execute("SELECT MAX(position) FROM segments WHERE project_id=?", (segment["project_id"],)).fetchone()[0]
        if segment["position"] != last:
            raise HTTPException(409, "为保护续拍缓存，只能删除最后一个片段")
        running = db.execute("SELECT 1 FROM jobs WHERE segment_id=? AND status IN ('queued','running')", (segment_id,)).fetchone()
        if running:
            raise HTTPException(409, "片段正在生成，不能删除")
        videos = rows(db.execute("SELECT * FROM videos WHERE segment_id=?", (segment_id,)).fetchall())
        references = rows(db.execute("SELECT * FROM segment_references WHERE segment_id=?", (segment_id,)).fetchall())
        db.execute("DELETE FROM segments WHERE id=?", (segment_id,))
    for video in videos:
        try:
            keyframes = json.loads(video.get("keyframes_json") or "[]")
        except json.JSONDecodeError:
            keyframes = []
        remove_video_assets(video["relative_path"], keyframes)
    for reference in references:
        (UPLOAD_DIR / reference["filename"]).unlink(missing_ok=True)
        (COMFY_INPUT_DIR.parent / reference["comfy_path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/segments/{segment_id}/generate")
def generate_segment(segment_id: int):
    with connect() as db:
        segment = get_segment(db, segment_id)
        refs = db.execute("SELECT COUNT(*) FROM character_references WHERE project_id=?", (segment["project_id"],)).fetchone()[0]
        segment_refs = db.execute("SELECT COUNT(*) FROM segment_references WHERE segment_id=?", (segment_id,)).fetchone()[0]
        if not refs and not segment_refs:
            raise HTTPException(409, "请先上传至少一张全局人物图或片段约束图")
        duplicate = db.execute("SELECT 1 FROM jobs WHERE segment_id=? AND status IN ('queued','running')", (segment_id,)).fetchone()
        if duplicate:
            raise HTTPException(409, "这个片段已经在生成队列中")
        if segment["continuity_mode"] == "continue" and segment.get("continuity_source_id"):
            source_ready = db.execute("SELECT 1 FROM videos WHERE segment_id=? LIMIT 1", (segment["continuity_source_id"],)).fetchone()
            source_pending = db.execute("SELECT 1 FROM jobs WHERE segment_id=? AND status IN ('queued','running') LIMIT 1", (segment["continuity_source_id"],)).fetchone()
            if not source_ready and not source_pending:
                raise HTTPException(409, "请先生成来源片段，或将来源片段加入生成队列")
        active_count = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        job_id = str(uuid.uuid4())
        phase = f"队列等待，第 {active_count + 1} 位" if active_count else "等待调度"
        db.execute(
            "INSERT INTO jobs(id,project_id,segment_id,status,progress,phase,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, segment["project_id"], segment_id, "queued", 0, phase, now_iso()),
        )
        db.execute("UPDATE segments SET status='queued',updated_at=? WHERE id=?", (now_iso(), segment_id))
    job_queue.put(job_id)
    return {"job_id": job_id, "status": "queued", "queue_position": active_count + 1}


@app.put("/api/projects/{project_id}/chain-order")
def update_chain_order(project_id: int, data: ChainOrderUpdate):
    with connect() as db:
        get_project(db, project_id)
        current = {row[0] for row in db.execute("SELECT id FROM segments WHERE project_id=?", (project_id,))}
        if set(data.segment_ids) != current or len(data.segment_ids) != len(current):
            raise HTTPException(422, "串联编号必须包含项目的全部片段且不能重复")
        for order, segment_id in enumerate(data.segment_ids, 1):
            db.execute("UPDATE segments SET chain_order=?,updated_at=? WHERE id=?", (order, now_iso(), segment_id))
    return {"ok": True}


@app.put("/api/segments/{segment_id}/merge-inclusion")
def update_merge_inclusion(segment_id: int, data: MergeInclusionUpdate):
    with connect() as db:
        segment = get_segment(db, segment_id)
        db.execute("UPDATE segments SET include_in_merge=?,updated_at=? WHERE id=?", (int(data.include_in_merge), now_iso(), segment_id))
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), segment["project_id"]))
    return {"segment_id": segment_id, "include_in_merge": data.include_in_merge}


@app.post("/api/projects/{project_id}/generate-chain")
def generate_chain(project_id: int):
    with connect() as db:
        get_project(db, project_id)
        segments = rows(db.execute("SELECT * FROM segments WHERE project_id=? AND include_in_merge=1 ORDER BY chain_order, position", (project_id,)).fetchall())
        if not segments:
            raise HTTPException(409, "请至少勾选一个参与合并的片段")
        selected = []
        for segment in segments:
            video = db.execute(
                "SELECT * FROM videos WHERE segment_id=? AND id=?",
                (segment["id"], segment.get("selected_video_id")),
            ).fetchone()
            if not video:
                raise HTTPException(409, f"片段{segment['position'] + 1}《{segment['title']}》没有选择用于合并的视频")
            selected.append(dict(video))
    paths = [(VIDEO_DIR / video["relative_path"]).resolve() for video in selected]
    if any(not path.is_file() or VIDEO_DIR.resolve() not in path.parents for path in paths):
        raise HTTPException(409, "部分片段视频文件不存在")
    combined_id = str(uuid.uuid4())
    relative = f"project_{project_id}/combined/{combined_id}.mp4"
    destination = VIDEO_DIR / relative
    try:
        media = concat_videos(paths, destination)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, f"FFmpeg合并失败：{exc}")
    with connect() as db:
        db.execute(
            "INSERT INTO combined_videos(id,project_id,filename,relative_path,source_video_ids_json,size_bytes,duration_seconds,width,height,video_frame_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (combined_id, project_id, destination.name, relative, json.dumps([video["id"] for video in selected]), destination.stat().st_size, media["duration_seconds"], media["width"], media["height"], media["video_frame_count"], now_iso()),
        )
        combined = dict(db.execute("SELECT * FROM combined_videos WHERE id=?", (combined_id,)).fetchone())
    combined["source_video_ids"] = json.loads(combined["source_video_ids_json"])
    return combined


@app.get("/api/combined-videos/{video_id}/stream")
def stream_combined_video(video_id: str, download: bool = False):
    with connect() as db:
        row = db.execute("SELECT * FROM combined_videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "合并视频不存在")
    path = (VIDEO_DIR / row["relative_path"]).resolve()
    if not path.is_file() or VIDEO_DIR.resolve() not in path.parents:
        raise HTTPException(404, "合并视频文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=row["filename"] if download else None, content_disposition_type="attachment" if download else "inline", headers={"Cache-Control": "no-store"})


@app.delete("/api/combined-videos/{video_id}")
def delete_combined_video(video_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM combined_videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            return {"ok": True}
        db.execute("DELETE FROM combined_videos WHERE id=?", (video_id,))
    path = (VIDEO_DIR / row["relative_path"]).resolve()
    if VIDEO_DIR.resolve() in path.parents:
        path.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    return dict(row)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    prompt_id = None
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "任务不存在")
        if job["status"] not in {"queued", "running"}:
            raise HTTPException(409, "任务已结束，不能取消")
        prompt_id = job["prompt_id"]

    if job["status"] == "running" and not prompt_id:
        for _ in range(20):
            threading.Event().wait(0.1)
            with connect() as db:
                current = db.execute("SELECT status,prompt_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not current or current["status"] not in {"queued", "running"}:
                raise HTTPException(409, "任务已结束，不能取消")
            prompt_id = current["prompt_id"]
            if prompt_id:
                break

    try:
        if prompt_id:
            running, pending = comfy_queue_prompt_ids()
            if prompt_id in running:
                response = httpx.post(f"{COMFY_HTTP}/interrupt", json={"prompt_id": prompt_id}, timeout=10)
                response.raise_for_status()
            elif prompt_id in pending:
                response = httpx.post(f"{COMFY_HTTP}/queue", json={"delete": [prompt_id]}, timeout=10)
                response.raise_for_status()
            elif comfy_history(prompt_id):
                raise HTTPException(409, "任务已完成，不能取消")
    except httpx.RequestError:
        raise HTTPException(502, "无法连接ComfyUI，取消失败")
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, f"ComfyUI取消失败（HTTP {error.response.status_code}）")

    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] not in {"queued", "running"}:
            raise HTTPException(409, "任务已结束，不能取消")
        stamp = now_iso()
        cancelled = db.execute(
            "UPDATE jobs SET status='cancelled',phase='已取消视频生成',error=NULL,finished_at=? WHERE id=? AND status IN ('queued','running')",
            (stamp, job_id),
        )
        if not cancelled.rowcount:
            raise HTTPException(409, "任务已结束，不能取消")
        has_video = db.execute("SELECT 1 FROM videos WHERE segment_id=? LIMIT 1", (job["segment_id"],)).fetchone()
        db.execute("UPDATE segments SET status=?,updated_at=? WHERE id=?", ("complete" if has_video else "draft", stamp, job["segment_id"]))
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/api/videos/{video_id}/stream")
def stream_video(video_id: int, download: bool = False):
    with connect() as db:
        row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "视频不存在")
    path = (VIDEO_DIR / row["relative_path"]).resolve()
    if not path.is_file() or VIDEO_DIR.resolve() not in path.parents:
        raise HTTPException(404, "视频文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=row["filename"] if download else None, content_disposition_type="attachment" if download else "inline", headers={"Cache-Control": "no-store"})


@app.put("/api/segments/{segment_id}/selected-video")
def select_segment_video(segment_id: int, data: VideoSelectionUpdate):
    with connect() as db:
        segment = get_segment(db, segment_id)
        video = db.execute("SELECT id FROM videos WHERE id=? AND segment_id=?", (data.video_id, segment_id)).fetchone()
        if not video:
            raise HTTPException(422, "所选视频不属于这个片段")
        db.execute("UPDATE segments SET selected_video_id=?,updated_at=? WHERE id=?", (data.video_id, now_iso(), segment_id))
    return {"segment_id": segment_id, "selected_video_id": data.video_id}


@app.get("/api/videos/{video_id}/keyframes/{index}")
def video_keyframe(video_id: int, index: int):
    with connect() as db:
        row = db.execute("SELECT keyframes_json FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "视频不存在")
    try:
        frames = json.loads(row["keyframes_json"] or "[]")
        frame = frames[index]
    except (json.JSONDecodeError, IndexError, TypeError):
        raise HTTPException(404, "关键帧不存在")
    path = (VIDEO_DIR / frame["relative_path"]).resolve()
    if not path.is_file() or VIDEO_DIR.resolve() not in path.parents:
        raise HTTPException(404, "关键帧文件不存在")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: int):
    with connect() as db:
        row = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            return {"ok": True}
        row = dict(row)
        was_selected = db.execute("SELECT selected_video_id FROM segments WHERE id=?", (row["segment_id"],)).fetchone()[0] == video_id
        db.execute("DELETE FROM videos WHERE id=?", (video_id,))
        if row["job_id"]:
            db.execute("DELETE FROM jobs WHERE id=? AND status NOT IN ('queued','running')", (row["job_id"],))
        has_video = db.execute("SELECT 1 FROM videos WHERE segment_id=? LIMIT 1", (row["segment_id"],)).fetchone()
        if was_selected and has_video:
            replacement = db.execute(
                """
                SELECT v.id FROM videos v LEFT JOIN jobs j ON j.id=v.job_id
                WHERE v.segment_id=?
                ORDER BY COALESCE(j.created_at,v.created_at) DESC,v.id DESC LIMIT 1
                """,
                (row["segment_id"],),
            ).fetchone()[0]
            db.execute("UPDATE segments SET selected_video_id=? WHERE id=?", (replacement, row["segment_id"]))
        db.execute("UPDATE segments SET status=?,updated_at=? WHERE id=?", ("complete" if has_video else "draft", now_iso(), row["segment_id"]))
    try:
        keyframes = json.loads(row["keyframes_json"] or "[]")
    except json.JSONDecodeError:
        keyframes = []
    remove_video_assets(row["relative_path"], keyframes)
    if row["job_id"]:
        comfy_job_dir = (COMFY_OUTPUT_DIR / "webapp_jobs" / row["job_id"]).resolve()
        if comfy_job_dir.parent == (COMFY_OUTPUT_DIR / "webapp_jobs").resolve():
            shutil.rmtree(comfy_job_dir, ignore_errors=True)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
