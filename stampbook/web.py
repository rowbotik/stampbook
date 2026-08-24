from __future__ import annotations

import threading
import os
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

from .core import connect, init_project, now_iso, process_pending, register_heif_if_available, scan_sources


def row_to_dict(row):
    item = dict(row)
    item["source_url"] = f"/media/source/{item['id']}"
    item["result_url"] = f"/media/result/{item['id']}" if item["thumbnail_path"] else None
    return item


def create_app(config):
    init_project(config)
    app = Flask(__name__)
    worker_lock = threading.Lock()
    stop_event = threading.Event()
    worker_state = {"running": False, "stopping": False, "last_results": []}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/jobs")
    def jobs():
        review = request.args.get("review", "all")
        status = request.args.get("status", "all")
        clauses, values = ["present = 1"], []
        if review != "all":
            clauses.append("review_status = ?")
            values.append(review)
        if status != "all":
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(config) as db:
            rows = db.execute(f"SELECT * FROM jobs {where} ORDER BY id", values).fetchall()
        return jsonify([row_to_dict(row) for row in rows])

    @app.get("/api/stats")
    def stats():
        with connect(config) as db:
            total = db.execute("SELECT COUNT(*) FROM jobs WHERE present = 1").fetchone()[0]
            statuses = dict(db.execute("SELECT status, COUNT(*) FROM jobs WHERE present = 1 GROUP BY status").fetchall())
            reviews = dict(db.execute("SELECT review_status, COUNT(*) FROM jobs WHERE present = 1 GROUP BY review_status").fetchall())
        return jsonify({
            "total": total,
            "statuses": statuses,
            "reviews": reviews,
            "api_key_available": bool(os.environ.get("OPENAI_API_KEY")),
            **worker_state,
        })

    @app.post("/api/scan")
    def scan():
        return jsonify(scan_sources(config))

    @app.post("/api/process")
    def process():
        if not os.environ.get("OPENAI_API_KEY"):
            return jsonify({
                "error": "The Stampbook server has no API key. Export OPENAI_API_KEY, then restart stampbook serve."
            }), 400
        body = request.get_json(silent=True) or {}
        limit = max(1, min(int(body.get("limit", 15)), 100))
        if not worker_lock.acquire(blocking=False):
            return jsonify({"error": "Processing is already running"}), 409
        stop_event.clear()
        worker_state["running"] = True
        worker_state["stopping"] = False

        def run():
            try:
                worker_state["last_results"] = process_pending(config, limit, should_stop=stop_event.is_set)
            finally:
                worker_state["running"] = False
                worker_state["stopping"] = False
                worker_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"started": True, "limit": limit}), 202

    @app.post("/api/stop")
    def stop():
        if not worker_state["running"]:
            return jsonify({"stopping": False, "message": "No batch is running"})
        stop_event.set()
        worker_state["stopping"] = True
        return jsonify({
            "stopping": True,
            "message": "Stop requested. The current image will finish; no new image will start."
        }), 202

    @app.post("/api/jobs/<int:job_id>/review")
    def review_job(job_id):
        body = request.get_json(silent=True) or {}
        value = body.get("review_status")
        if value not in {"unreviewed", "approved", "rejected"}:
            return jsonify({"error": "Invalid review status"}), 400
        note = str(body.get("note", ""))[:1000]
        with connect(config) as db:
            cursor = db.execute(
                "UPDATE jobs SET review_status = ?, note = ?, updated_at = ? WHERE id = ?",
                (value, note, now_iso(), job_id),
            )
        if not cursor.rowcount:
            abort(404)
        return jsonify({"id": job_id, "review_status": value, "note": note})

    @app.post("/api/jobs/<int:job_id>/regenerate")
    def regenerate(job_id):
        body = request.get_json(silent=True) or {}
        note = str(body.get("note", ""))[:1000]
        with connect(config) as db:
            cursor = db.execute(
                """UPDATE jobs SET status = 'pending', attempts = 0, error = NULL,
                review_status = 'unreviewed', note = ?, updated_at = ? WHERE id = ?""",
                (note, now_iso(), job_id),
            )
        if not cursor.rowcount:
            abort(404)
        return jsonify({"id": job_id, "status": "pending"})

    def media_path(job_id: int, column: str) -> Path:
        with connect(config) as db:
            row = db.execute(f"SELECT {column} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or not row[column]:
            abort(404)
        path = Path(row[column]).resolve()
        if not path.is_file():
            abort(404)
        return path

    @app.get("/media/source/<int:job_id>")
    def source_media(job_id):
        register_heif_if_available()
        path = media_path(job_id, "source_path")
        with Image.open(path) as image:
            preview = ImageOps.exif_transpose(image).convert("RGB")
            preview.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            data = BytesIO()
            preview.save(data, "JPEG", quality=88, optimize=True)
        data.seek(0)
        return send_file(data, mimetype="image/jpeg", max_age=3600)

    @app.get("/media/result/<int:job_id>")
    def result_media(job_id):
        return send_file(media_path(job_id, "thumbnail_path"), conditional=True)

    return app
