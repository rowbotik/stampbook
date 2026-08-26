import json
from pathlib import Path

from PIL import Image

from stampbook.core import (
    build_prompt,
    connect,
    get_art_mode,
    image_edit_parameters,
    load_config,
    process_one,
    process_pending,
    scan_sources,
)
from stampbook.web import create_app


def project(tmp_path: Path):
    config_path = tmp_path / "config.json"
    (tmp_path / "prompt.txt").write_text("test prompt", encoding="utf-8")
    config_path.write_text(json.dumps({
        "source_dir": "source", "output_dir": "output", "database": "stampbook.db",
        "prompt_file": "prompt.txt", "model": "gpt-image-2", "size": "1024x1024",
        "quality": "medium", "max_attempts": 3,
    }), encoding="utf-8")
    return load_config(config_path)


def test_scan_is_idempotent_and_detects_changes(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    photo = config["source_dir"] / "coast.jpg"
    Image.new("RGB", (80, 60), "navy").save(photo)
    assert scan_sources(config) == {"found": 1, "added": 1, "updated": 0, "unchanged": 0, "missing": 0}
    assert scan_sources(config) == {"found": 1, "added": 0, "updated": 0, "unchanged": 1, "missing": 0}
    Image.new("RGB", (90, 60), "green").save(photo)
    assert scan_sources(config)["updated"] == 1


def test_dry_run_validates_without_consuming_attempt(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    Image.new("RGB", (80, 60), "navy").save(config["source_dir"] / "coast.jpg")
    scan_sources(config)
    result = process_pending(config, limit=1, dry_run=True)
    assert result == [{"id": 1, "status": "validated"}]
    with connect(config) as db:
        job = db.execute("SELECT status, attempts FROM jobs WHERE id = 1").fetchone()
    assert dict(job) == {"status": "pending", "attempts": 0}


def test_review_api_and_source_preview(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    Image.new("RGB", (80, 60), "navy").save(config["source_dir"] / "coast.tiff")
    scan_sources(config)
    client = create_app(config).test_client()
    jobs = client.get("/api/jobs").get_json()
    assert jobs[0]["source_name"] == "coast.tiff"
    preview = client.get("/media/source/1")
    assert preview.status_code == 200
    assert preview.mimetype == "image/jpeg"
    response = client.post("/api/jobs/1/review", json={"review_status": "rejected", "note": "Simplify the cliff"})
    assert response.status_code == 200
    assert response.get_json()["note"] == "Simplify the cliff"


def test_gpt_image_2_request_omits_unsupported_input_fidelity(tmp_path):
    config = project(tmp_path)
    parameters = image_edit_parameters(config, object(), "prompt")
    assert parameters["model"] == "gpt-image-2"
    assert parameters["background"] == "transparent"
    assert "input_fidelity" not in parameters


def test_web_prevents_batch_without_api_key(tmp_path, monkeypatch):
    config = project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = create_app(config).test_client()
    stats = client.get("/api/stats").get_json()
    assert stats["api_key_available"] is False
    response = client.post("/api/process", json={"limit": 1})
    assert response.status_code == 400
    assert "restart" in response.get_json()["error"].lower()


def test_stop_without_running_batch_is_safe(tmp_path):
    config = project(tmp_path)
    client = create_app(config).test_client()
    response = client.post("/api/stop")
    assert response.status_code == 200
    assert response.get_json() == {"message": "No batch is running", "stopping": False}


def test_process_pending_checks_stop_before_starting_job(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    Image.new("RGB", (80, 60), "navy").save(config["source_dir"] / "coast.jpg")
    scan_sources(config)
    assert process_pending(config, limit=1, dry_run=True, should_stop=lambda: True) == []
    with connect(config) as db:
        job = db.execute("SELECT status, attempts FROM jobs WHERE id = 1").fetchone()
    assert dict(job) == {"status": "pending", "attempts": 0}


def test_rescan_hides_missing_sources_without_deleting_history(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    photo = config["source_dir"] / "coast.jpg"
    Image.new("RGB", (80, 60), "navy").save(photo)
    scan_sources(config)
    photo.unlink()
    result = scan_sources(config)
    assert result == {"found": 0, "added": 0, "updated": 0, "unchanged": 0, "missing": 1}
    with connect(config) as db:
        job = db.execute("SELECT present, status, source_name FROM jobs WHERE id = 1").fetchone()
    assert dict(job) == {"present": 0, "status": "missing", "source_name": "coast.jpg"}
    assert create_app(config).test_client().get("/api/jobs").get_json() == []

    Image.new("RGB", (80, 60), "navy").save(photo)
    scan_sources(config)
    with connect(config) as db:
        restored = db.execute("SELECT present, status, attempts, error FROM jobs WHERE id = 1").fetchone()
    assert dict(restored) == {"present": 1, "status": "pending", "attempts": 0, "error": None}


def test_queued_job_removed_after_source_disappears(tmp_path):
    config = project(tmp_path)
    config["source_dir"].mkdir()
    photo = config["source_dir"] / "coast.jpg"
    Image.new("RGB", (80, 60), "navy").save(photo)
    scan_sources(config)
    photo.unlink()
    result = process_one(config, 1, dry_run=True)
    assert result == {"id": 1, "status": "missing"}
    with connect(config) as db:
        job = db.execute("SELECT present, status, attempts, error FROM jobs WHERE id = 1").fetchone()
    assert dict(job) == {"present": 0, "status": "missing", "attempts": 0, "error": None}


def test_art_mode_defaults_and_persists(tmp_path):
    config = project(tmp_path)
    client = create_app(config).test_client()
    assert get_art_mode(config) == "source"
    response = client.post("/api/mode", json={"art_mode": "rgb"})
    assert response.status_code == 200
    assert response.get_json() == {"art_mode": "rgb"}
    assert get_art_mode(config) == "rgb"
    assert client.get("/api/stats").get_json()["art_mode"] == "rgb"


def test_rgb_mode_adds_strict_three_ink_prompt(tmp_path):
    config = project(tmp_path)
    prompt = build_prompt(config, "", "rgb")
    assert "exactly three spot inks only" in prompt
    assert "#FF0000" in prompt
    assert "#00A000" in prompt
    assert "#0057FF" in prompt
    assert "RGB MODE OVERRIDE" not in build_prompt(config, "", "source")
