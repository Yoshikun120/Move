"""เซิร์ฟเวอร์เว็บ: รับไฟล์วิดีโอ -> รันสายพานเบื้องหลัง -> ให้ดูความคืบหน้าและดาวน์โหลด mp4"""
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import pipeline

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
MAX_MB = 2000

app = FastAPI(title="AI สรุปหนัง")
pool = ThreadPoolExecutor(max_workers=1)  # ตัดต่อทีละงาน กัน CPU เต็ม
jobs = {}
lock = threading.Lock()


def update(job_id, **kw):
    with lock:
        jobs[job_id].update(kw)


def work(job_id, video, style, length, voice):
    wd = JOBS_DIR / job_id
    try:
        def prog(p, m):
            update(job_id, progress=p, message=m)

        final, plan = pipeline.process(video, wd, style, length, voice, prog)
        update(job_id, status="done", result=str(final), plan=plan)
    except Exception as e:
        traceback.print_exc()
        update(job_id, status="error", message=str(e)[:800])


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "index.html").read_text(encoding="utf-8")


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    style: str = Form("spoil"),
    length: int = Form(60),
    voice: str = Form("female"),
):
    if not (file.filename or "").lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")):
        raise HTTPException(400, "รองรับเฉพาะไฟล์วิดีโอ mp4/mov/mkv/webm/avi")
    job_id = uuid.uuid4().hex[:10]
    wd = JOBS_DIR / job_id
    wd.mkdir(parents=True)
    video = wd / ("source" + Path(file.filename).suffix.lower())
    size = 0
    with open(video, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MB * 1024 * 1024:
                f.close()
                shutil.rmtree(wd, ignore_errors=True)
                raise HTTPException(413, f"ไฟล์ใหญ่เกิน {MAX_MB} MB")
            f.write(chunk)
    length = min(max(length, 20), 180)
    jobs[job_id] = {"status": "running", "progress": 1, "message": "เข้าคิว..."}
    pool.submit(work, job_id, video, style, length, voice)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def status(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "ไม่พบงาน")
    out = {k: v for k, v in j.items() if k != "result"}
    if j["status"] == "done":
        out["download"] = f"/api/jobs/{job_id}/video"
    return out


@app.get("/api/jobs/{job_id}/video")
def video(job_id: str):
    j = jobs.get(job_id)
    if not j or j["status"] != "done":
        raise HTTPException(404, "ยังไม่พร้อม")
    return FileResponse(j["result"], media_type="video/mp4", filename=f"recap_{job_id}.mp4")
