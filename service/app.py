"""
Local HTTP service wrapping Project Scribe's generation and evaluation
pipeline for the Overleaf extension.

Run from the repo root so pipeline modules' load_dotenv() finds .env:

    ./my_venv/bin/uvicorn service.app:app --port 8765

Job-based rather than blocking: abstract enrichment sleeps ~3s per
uncached bib entry (no S2_API_KEY) and Tier 4 judge calls have a
multi-minute worst-case tail (see pipeline/evaluator.py). A request
starts a job and returns immediately; the extension polls GET /jobs/{id}.
"""

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .jobs import create_job, get_job
from .tasks import run_check_job, run_generate_job

app = FastAPI(title="Scribe Extension Service")

# The extension calls from a chrome-extension:// origin, which is not
# same-origin with localhost — a real cross-origin request even though
# both sides are on this machine. Extension ids are unstable during
# unpacked development, so this allows the whole chrome-extension scheme
# rather than pinning one id; this service only ever binds to 127.0.0.1
# and does nothing an origin couldn't already do by opening a tab to it.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    tex_source: str
    bib_content: str
    target_word_count: int | None = None
    model: str | None = None


class CheckRequest(BaseModel):
    text: str
    bib_content: str


@app.get("/health")
def health():
    return {"ok": True, "has_key": bool(os.getenv("OPENROUTER_API_KEY"))}


@app.post("/jobs/generate")
def start_generate_job(req: GenerateRequest):
    job = create_job(
        lambda job: run_generate_job(
            job, req.tex_source, req.bib_content, req.target_word_count, req.model
        )
    )
    return {"job_id": job.id}


@app.post("/jobs/check")
def start_check_job(req: CheckRequest):
    job = create_job(lambda job: run_check_job(job, req.text, req.bib_content))
    return {"job_id": job.id}


@app.get("/jobs/{job_id}")
def poll_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return job.to_dict()
