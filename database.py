import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt, digest


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db():
    with connect() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                character_prompt TEXT NOT NULL DEFAULT '',
                environment_prompt TEXT NOT NULL DEFAULT '',
                continuity_negative_prompt TEXT NOT NULL DEFAULT '',
                visual_prompt TEXT NOT NULL DEFAULT '',
                audio_prompt TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 608,
                height INTEGER NOT NULL DEFAULT 352,
                fps INTEGER NOT NULL DEFAULT 24,
                steps INTEGER NOT NULL DEFAULT 16,
                seed INTEGER NOT NULL DEFAULT 42,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS character_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                slot INTEGER NOT NULL CHECK(slot IN (1, 2)),
                filename TEXT NOT NULL,
                comfy_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, slot)
            );

            CREATE TABLE IF NOT EXISTS segment_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                slot INTEGER NOT NULL CHECK(slot IN (1, 2, 3, 4, 5)),
                filename TEXT NOT NULL,
                comfy_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(segment_id, slot)
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                duration INTEGER NOT NULL CHECK(duration IN (2, 3, 5, 6, 8, 10, 12, 15, 18, 20)),
                frame_count INTEGER NOT NULL,
                continuity_mode TEXT NOT NULL CHECK(continuity_mode IN ('continue', 'cut')),
                story_direction TEXT NOT NULL DEFAULT '',
                world_prompt TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL,
                ending_prompt TEXT NOT NULL DEFAULT '',
                refine_enabled INTEGER NOT NULL DEFAULT 0,
                refine_denoise REAL NOT NULL DEFAULT 0.25,
                refine_steps INTEGER NOT NULL DEFAULT 10,
                continuity_source_id INTEGER REFERENCES segments(id) ON DELETE SET NULL,
                chain_order INTEGER,
                selected_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                include_in_merge INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, position)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                prompt_id TEXT,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                phase TEXT NOT NULL DEFAULT '等待调度',
                error TEXT,
                output_path TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                filename TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                duration_seconds REAL,
                refined INTEGER NOT NULL DEFAULT 0,
                width INTEGER,
                height INTEGER,
                video_frame_count INTEGER,
                keyframes_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS combined_videos (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                source_video_ids_json TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                duration_seconds REAL,
                width INTEGER,
                height INTEGER,
                video_frame_count INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model_name TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_segments_project ON segments(project_id, position);
            CREATE INDEX IF NOT EXISTS idx_segment_references_segment ON segment_references(segment_id, slot);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_videos_project ON videos(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_combined_videos_project ON combined_videos(project_id, created_at DESC);
            """
        )
        stamp = now_iso()
        missing_users = [(username, is_admin) for username, is_admin in (("admin", 1), ("leo", 0))
                         if not db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()]
        initial_password = os.environ.get("SCENEFLOW_INITIAL_PASSWORD") if missing_users else None
        if missing_users and (not initial_password or len(initial_password) < 12):
            raise RuntimeError("Set SCENEFLOW_INITIAL_PASSWORD (at least 12 characters) before the first start")
        for username, is_admin in missing_users:
            salt, digest = create_password(initial_password)
            db.execute(
                "INSERT INTO users(username,password_salt,password_hash,is_admin,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (username, salt, digest, is_admin, 1, stamp, stamp),
            )
        project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)").fetchall()}
        if "user_id" not in project_columns:
            db.execute("ALTER TABLE projects ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        leo_id = db.execute("SELECT id FROM users WHERE username='leo'").fetchone()[0]
        db.execute("UPDATE projects SET user_id=? WHERE user_id IS NULL", (leo_id,))
        db.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, updated_at DESC)")
        segment_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='segments'"
        ).fetchone()[0]
        if "2, 3, 5, 6, 8, 10, 12, 15, 18, 20" not in segment_sql:
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(
                """
                CREATE TABLE segments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    duration INTEGER NOT NULL CHECK(duration IN (2, 3, 5, 6, 8, 10, 12, 15, 18, 20)),
                    frame_count INTEGER NOT NULL,
                    continuity_mode TEXT NOT NULL CHECK(continuity_mode IN ('continue', 'cut')),
                    prompt TEXT NOT NULL,
                    ending_prompt TEXT NOT NULL DEFAULT '',
                    refine_enabled INTEGER NOT NULL DEFAULT 0,
                    refine_denoise REAL NOT NULL DEFAULT 0.25,
                    refine_steps INTEGER NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, position)
                );
                INSERT INTO segments_new(
                    id, project_id, position, title, duration, frame_count,
                    continuity_mode, prompt, ending_prompt,
                    refine_enabled, refine_denoise, refine_steps,
                    status, created_at, updated_at
                )
                SELECT id, project_id, position, title, duration, frame_count,
                       continuity_mode, prompt, ending_prompt,
                       refine_enabled, refine_denoise, refine_steps,
                       status, created_at, updated_at
                FROM segments;
                DROP TABLE segments;
                ALTER TABLE segments_new RENAME TO segments;
                CREATE INDEX IF NOT EXISTS idx_segments_project ON segments(project_id, position);
                """
            )
            db.execute("PRAGMA foreign_keys = ON")
        segment_reference_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='segment_references'").fetchone()[0]
        if "1, 2, 3, 4, 5" not in segment_reference_sql:
            db.execute("PRAGMA foreign_keys = OFF")
            db.executescript(
                """
                CREATE TABLE segment_references_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                    slot INTEGER NOT NULL CHECK(slot IN (1, 2, 3, 4, 5)),
                    filename TEXT NOT NULL,
                    comfy_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(segment_id, slot)
                );
                INSERT INTO segment_references_new(id,segment_id,slot,filename,comfy_path,created_at)
                SELECT id,segment_id,slot,filename,comfy_path,created_at FROM segment_references;
                DROP TABLE segment_references;
                ALTER TABLE segment_references_new RENAME TO segment_references;
                CREATE INDEX IF NOT EXISTS idx_segment_references_segment ON segment_references(segment_id, slot);
                """
            )
            db.execute("PRAGMA foreign_keys = ON")
        segment_columns = {
            row[1] for row in db.execute("PRAGMA table_info(segments)").fetchall()
        }
        if "refine_enabled" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN refine_enabled INTEGER NOT NULL DEFAULT 0")
        if "refine_denoise" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN refine_denoise REAL NOT NULL DEFAULT 0.25")
        if "refine_steps" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN refine_steps INTEGER NOT NULL DEFAULT 10")
        if "continuity_source_id" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN continuity_source_id INTEGER REFERENCES segments(id) ON DELETE SET NULL")
            db.execute(
                """
                UPDATE segments
                SET continuity_source_id = (
                    SELECT previous.id FROM segments AS previous
                    WHERE previous.project_id = segments.project_id
                      AND previous.position = segments.position - 1
                )
                WHERE continuity_mode = 'continue' AND position > 0
                """
            )
        if "chain_order" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN chain_order INTEGER")
            db.execute("UPDATE segments SET chain_order = position + 1 WHERE chain_order IS NULL")
        if "story_direction" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN story_direction TEXT NOT NULL DEFAULT ''")
        if "world_prompt" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN world_prompt TEXT NOT NULL DEFAULT ''")
        if "selected_video_id" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN selected_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL")
            db.execute(
                """
                UPDATE segments SET selected_video_id = (
                    SELECT v.id FROM videos v LEFT JOIN jobs j ON j.id=v.job_id
                    WHERE v.segment_id=segments.id
                    ORDER BY COALESCE(j.created_at,v.created_at) DESC,v.id DESC LIMIT 1
                )
                """
            )
        if "include_in_merge" not in segment_columns:
            db.execute("ALTER TABLE segments ADD COLUMN include_in_merge INTEGER NOT NULL DEFAULT 1")
        project_columns = {
            row[1] for row in db.execute("PRAGMA table_info(projects)").fetchall()
        }
        if "continuity_negative_prompt" not in project_columns:
            db.execute("ALTER TABLE projects ADD COLUMN continuity_negative_prompt TEXT NOT NULL DEFAULT ''")
        video_columns = {
            row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
        }
        video_additions = {
            "duration_seconds": "REAL",
            "refined": "INTEGER NOT NULL DEFAULT 0",
            "width": "INTEGER",
            "height": "INTEGER",
            "video_frame_count": "INTEGER",
            "keyframes_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in video_additions.items():
            if name not in video_columns:
                db.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")


def rows(rows):
    return [dict(row) for row in rows]
