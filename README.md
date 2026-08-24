# Stampbook Review Desk

A local, resumable batch processor that turns reference photographs into standalone travel rubber-stamp assets using OpenAI image editing. Originals are indexed read-only and never modified.

## Set up

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test,heic]'
stampbook init
```

Put photographs in `source/`. The default output and database locations are in `config.json`.

## Validate before spending

```bash
stampbook scan
stampbook process --limit 15 --dry-run
pytest
```

The dry run opens and normalizes each input but makes no API request.

## Run a paid pilot

Set the API key only in your shell; do not put it in `config.json`:

```bash
export OPENAI_API_KEY='your-key-here'
stampbook process --limit 15
```

The default is one request at a time. Each successful job creates:

- `output/transparent/`: final transparent PNG
- `output/white/`: final PNG composited on pure white
- `output/thumbnails/`: smaller review image

## Review visually

```bash
stampbook serve
```

Open [http://127.0.0.1:7331](http://127.0.0.1:7331). Scan for newly added files, process a controlled number, approve or reject proofs, and queue individual records for regeneration. A correction note is appended only to that photograph's next generation; the shared `prompt.txt` remains the base prompt for consistent batches.

## Safety and recovery

- Source files are never written or deleted.
- Processing resumes from `stampbook.db`.
- Interrupted jobs become failed jobs and can retry up to `max_attempts`.
- Queueing regeneration does not delete the existing output; it is replaced only after a successful new generation.
- Changing a source file returns its record to pending review.
- Changing `prompt.txt` affects future runs; completed outputs remain untouched.

Back up `stampbook.db`, `prompt.txt`, and `output/` with the photo-book project.
