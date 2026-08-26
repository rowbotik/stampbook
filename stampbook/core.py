from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
ART_MODES = {"source", "rgb"}
RGB_PROMPT_OVERRIDE = """

RGB MODE OVERRIDE:
Ignore the source photograph's color palette. Print the stamp with exactly three spot inks only: pure red (#FF0000), pure green (#00A000), and pure blue (#0057FF). Use all three inks as independently carved and hand-pressed layers. Do not introduce black, gray, brown, ochre, cyan, magenta, yellow, white ink, or any additional color. Transparent gaps and dry-ink paper show-through are allowed and required. Keep the result recognizably rubber-stamped, not a smooth RGB digital illustration.
""".strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.parent
    for key in ("source_dir", "output_dir", "database", "prompt_file"):
        path = Path(config[key]).expanduser()
        config[key] = (base / path).resolve() if not path.is_absolute() else path.resolve()
    return config


def prompt_digest(prompt_path: Path) -> str:
    return hashlib.sha256(prompt_path.read_bytes()).hexdigest()[:12]


@contextmanager
def connect(config: dict[str, Any]) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(config["database"], timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_project(config: dict[str, Any]) -> None:
    config["source_dir"].mkdir(parents=True, exist_ok=True)
    for folder in ("transparent", "white", "thumbnails"):
        (config["output_dir"] / folder).mkdir(parents=True, exist_ok=True)
    with connect(config) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                source_name TEXT NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                transparent_path TEXT,
                white_path TEXT,
                thumbnail_path TEXT,
                error TEXT,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                note TEXT NOT NULL DEFAULT '',
                prompt_sha TEXT,
                present INTEGER NOT NULL DEFAULT 1,
                art_mode TEXT NOT NULL DEFAULT 'source',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "present" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN present INTEGER NOT NULL DEFAULT 1")
        if "art_mode" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN art_mode TEXT NOT NULL DEFAULT 'source'")
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('art_mode', 'source')"
        )
        db.execute("CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status)")
        db.execute("CREATE INDEX IF NOT EXISTS jobs_review_idx ON jobs(review_status)")


def scan_sources(config: dict[str, Any]) -> dict[str, int]:
    init_project(config)
    files = sorted(
        path for path in config["source_dir"].rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    added = updated = unchanged = 0
    with connect(config) as db:
        db.execute("UPDATE jobs SET present = 0")
        for path in files:
            stat = path.stat()
            row = db.execute("SELECT * FROM jobs WHERE source_path = ?", (str(path.resolve()),)).fetchone()
            stamp = now_iso()
            if row is None:
                db.execute(
                    """INSERT INTO jobs
                    (source_path, source_name, source_mtime_ns, source_size, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (str(path.resolve()), path.name, stat.st_mtime_ns, stat.st_size, stamp, stamp),
                )
                added += 1
            elif row["source_mtime_ns"] != stat.st_mtime_ns or row["source_size"] != stat.st_size:
                db.execute(
                    """UPDATE jobs SET source_mtime_ns = ?, source_size = ?, status = 'pending',
                    review_status = 'unreviewed', error = NULL, present = 1, updated_at = ? WHERE id = ?""",
                    (stat.st_mtime_ns, stat.st_size, stamp, row["id"]),
                )
                updated += 1
            else:
                was_missing = row["status"] == "missing" or "No such file or directory" in (row["error"] or "")
                if was_missing:
                    db.execute(
                        """UPDATE jobs SET present = 1, status = 'pending', attempts = 0,
                        error = NULL, updated_at = ? WHERE id = ?""",
                        (stamp, row["id"]),
                    )
                else:
                    db.execute("UPDATE jobs SET present = 1 WHERE id = ?", (row["id"],))
                unchanged += 1
        db.execute(
            """UPDATE jobs SET status = 'missing', error = NULL, updated_at = ?
            WHERE present = 0 AND status != 'complete'""",
            (now_iso(),),
        )
        missing = db.execute("SELECT COUNT(*) FROM jobs WHERE present = 0").fetchone()[0]
    return {"found": len(files), "added": added, "updated": updated, "unchanged": unchanged, "missing": missing}


def slug_for_job(job: sqlite3.Row) -> str:
    stem = Path(job["source_name"]).stem
    safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in stem)
    safe = "-".join(part for part in safe.split("-") if part)[:80] or "photo"
    return f"{job['id']:05d}-{safe}"


def register_heif_if_available() -> None:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


@contextmanager
def normalized_input(source_path: Path) -> Iterator[Path]:
    register_heif_if_available()
    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            image.save(temporary_path, format="PNG", optimize=True)
    except Exception as error:
        if source_path.suffix.lower() in {".heic", ".heif"}:
            raise RuntimeError("HEIC support requires: pip install -e '.[heic]'") from error
        raise
    try:
        yield temporary_path
    finally:
        temporary_path.unlink(missing_ok=True)


def get_art_mode(config: dict[str, Any]) -> str:
    init_project(config)
    with connect(config) as db:
        row = db.execute("SELECT value FROM settings WHERE key = 'art_mode'").fetchone()
    return row["value"] if row and row["value"] in ART_MODES else "source"


def set_art_mode(config: dict[str, Any], art_mode: str) -> str:
    if art_mode not in ART_MODES:
        raise ValueError(f"Unknown art mode: {art_mode}")
    init_project(config)
    with connect(config) as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('art_mode', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (art_mode,),
        )
    return art_mode


def build_prompt(config: dict[str, Any], note: str, art_mode: str) -> str:
    prompt = config["prompt_file"].read_text(encoding="utf-8")
    if art_mode == "rgb":
        prompt += f"\n\n{RGB_PROMPT_OVERRIDE}"
    if note.strip():
        prompt += f"\n\nCorrection for this photograph only:\n{note.strip()}"
    return prompt


def write_derivatives(
    config: dict[str, Any], job: sqlite3.Row, image_bytes: bytes, art_mode: str
) -> dict[str, str]:
    slug = f"{slug_for_job(job)}-{art_mode}"
    transparent_path = config["output_dir"] / "transparent" / f"{slug}.png"
    white_path = config["output_dir"] / "white" / f"{slug}.png"
    thumbnail_path = config["output_dir"] / "thumbnails" / f"{slug}.jpg"

    with Image.open(__import__("io").BytesIO(image_bytes)) as generated:
        transparent = generated.convert("RGBA")
        transparent.save(transparent_path, "PNG", optimize=True)
        white = Image.new("RGBA", transparent.size, "white")
        white.alpha_composite(transparent)
        white.convert("RGB").save(white_path, "PNG", optimize=True)
        review = white.convert("RGB")
        review.thumbnail((720, 720), Image.Resampling.LANCZOS)
        review.save(thumbnail_path, "JPEG", quality=88, optimize=True)

    return {
        "transparent_path": str(transparent_path),
        "white_path": str(white_path),
        "thumbnail_path": str(thumbnail_path),
    }


def image_edit_parameters(config: dict[str, Any], image_file, prompt: str) -> dict[str, Any]:
    """Build only the parameters supported by the configured image model."""
    return {
        "model": config["model"],
        "image": image_file,
        "prompt": prompt,
        "size": config["size"],
        "quality": config["quality"],
        "background": "transparent",
        "output_format": "png",
    }


def process_one(
    config: dict[str, Any],
    job_id: int,
    dry_run: bool = False,
    art_mode: str | None = None,
) -> dict[str, Any]:
    if not dry_run and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not available to the Stampbook server")
    selected_mode = art_mode or get_art_mode(config)
    if selected_mode not in ART_MODES:
        raise ValueError(f"Unknown art mode: {selected_mode}")
    with connect(config) as db:
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise ValueError(f"Unknown job id: {job_id}")
        if not job["present"] or not Path(job["source_path"]).is_file():
            db.execute(
                "UPDATE jobs SET present = 0, status = 'missing', error = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), job_id),
            )
            return {"id": job_id, "status": "missing"}
        if job["attempts"] >= int(config["max_attempts"]):
            raise RuntimeError(f"Job {job_id} reached max_attempts; reset it before retrying")
        db.execute(
            """UPDATE jobs SET status = 'processing', attempts = attempts + 1, error = NULL,
            art_mode = ?, updated_at = ? WHERE id = ?""",
            (selected_mode, now_iso(), job_id),
        )

    try:
        source_path = Path(job["source_path"])
        with normalized_input(source_path) as image_path:
            if dry_run:
                with Image.open(image_path) as image:
                    image.verify()
                with connect(config) as db:
                    db.execute("UPDATE jobs SET status = 'pending', attempts = attempts - 1, updated_at = ? WHERE id = ?", (now_iso(), job_id))
                return {"id": job_id, "status": "validated"}

            from openai import OpenAI

            prompt = build_prompt(config, job["note"], selected_mode)
            client = OpenAI()
            with image_path.open("rb") as image_file:
                result = client.images.edit(**image_edit_parameters(config, image_file, prompt))
            encoded = result.data[0].b64_json
            if not encoded:
                raise RuntimeError("Image API returned no image data")
            paths = write_derivatives(config, job, base64.b64decode(encoded), selected_mode)

        with connect(config) as db:
            db.execute(
                """UPDATE jobs SET status = 'complete', transparent_path = ?, white_path = ?,
                thumbnail_path = ?, prompt_sha = ?, error = NULL, updated_at = ? WHERE id = ?""",
                (*paths.values(), prompt_digest(config["prompt_file"]), now_iso(), job_id),
            )
        return {"id": job_id, "status": "complete", "art_mode": selected_mode, **paths}
    except Exception as error:
        with connect(config) as db:
            db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (str(error)[:1000], now_iso(), job_id),
            )
        raise


def process_pending(
    config: dict[str, Any],
    limit: int,
    dry_run: bool = False,
    should_stop: Callable[[], bool] | None = None,
    art_mode: str | None = None,
) -> list[dict[str, Any]]:
    selected_mode = art_mode or get_art_mode(config)
    with connect(config) as db:
        rows = db.execute(
            "SELECT id FROM jobs WHERE present = 1 AND status IN ('pending', 'failed') AND attempts < ? ORDER BY id LIMIT ?",
            (int(config["max_attempts"]), limit),
        ).fetchall()
    results = []
    for row in rows:
        if should_stop and should_stop():
            break
        try:
            results.append(process_one(config, row["id"], dry_run=dry_run, art_mode=selected_mode))
        except Exception as error:
            results.append({"id": row["id"], "status": "failed", "error": str(error)})
    return results


def recover_interrupted(config: dict[str, Any]) -> int:
    with connect(config) as db:
        cursor = db.execute(
            "UPDATE jobs SET status = 'failed', error = 'Interrupted before completion', updated_at = ? WHERE status = 'processing'",
            (now_iso(),),
        )
        return cursor.rowcount
