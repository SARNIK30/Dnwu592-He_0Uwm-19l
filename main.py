"""
main.py — FastAPI + Telegram Webhook + Web Interface
=====================================================
Запуск:
    ADMIN_PASSWORD=secret BOT_TOKEN=xxx WEBHOOK_URL=https://yourdomain.com uvicorn main:app --host 0.0.0.0 --port 8000

Установка webhook (один раз):
    curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook" -d "url=https://yourdomain.com/webhook"
"""

import os
import re
import json
import time
import uuid
import glob
import hmac
import hashlib
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Set, List
from pathlib import Path

import yt_dlp
import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")        # опционально, для верификации
ADMIN_PASSWORD   = os.getenv("ADMIN_PASSWORD", "admin123")
AD_TEXT          = os.getenv("AD_TEXT", "").strip()
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")           # https://yourdomain.com

ADMIN_IDS: Set[int] = set(
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
)

MAX_MB           = 50
MAX_BYTES        = MAX_MB * 1024 * 1024
COOLDOWN_SECONDS = 12
MAX_QUEUE_PER_USER = 2
GLOBAL_QUEUE_LIMIT = 100

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CACHE_FILE     = "cache.json"
BANS_FILE      = "bans.json"
STATS_FILE     = "stats.json"
HISTORY_FILE   = "history.json"
USERS_FILE     = "users.json"
DOWNLOADS_DIR  = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

# ──────────────────────────────────────────────────────────────
# STORAGE HELPERS
# ──────────────────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# In-memory state
url_cache: Dict[str, str]  = load_json(CACHE_FILE, {})   # url -> filepath
banned_ids: Set[int]        = set(load_json(BANS_FILE, []))
stats: Dict[str, Any]       = load_json(STATS_FILE, {
    "total_requests": 0,
    "served_from_cache": 0,
    "downloads_ok": 0,
    "blocked_big": 0,
    "errors": 0,
})

_raw_users = load_json(USERS_FILE, [])
users: Set[int] = {int(x) for x in _raw_users if str(x).lstrip("-").isdigit()}

# History: list of dicts {url, status, size_mb, ts, source}
history: List[Dict] = load_json(HISTORY_FILE, [])
HISTORY_LIMIT = 200

def save_users():
    save_json(USERS_FILE, sorted(list(users)))

def add_history(url: str, status: str, size_mb: float = 0, source: str = "web"):
    history.insert(0, {
        "url": url[:120],
        "status": status,
        "size_mb": round(size_mb, 1),
        "ts": int(time.time()),
        "source": source,
    })
    if len(history) > HISTORY_LIMIT:
        history[:] = history[:HISTORY_LIMIT]
    save_json(HISTORY_FILE, history)

# ──────────────────────────────────────────────────────────────
# YT-DLP HELPERS
# ──────────────────────────────────────────────────────────────
def extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return re.sub(r"[)\]}>,.]+$", "", m.group(1))

def url_key(url: str) -> str:
    return url.strip()

def safe_cleanup(prefix: str):
    for p in glob.glob(prefix + ".*"):
        try:
            os.remove(p)
        except Exception:
            pass

def ytdlp_probe(url: str) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)

def estimate_size_bytes(info: Dict) -> Optional[int]:
    for k in ("filesize", "filesize_approx"):
        v = info.get(k)
        if isinstance(v, int) and v > 0:
            return v
    best = None
    for f in (info.get("formats") or []):
        if f.get("filesize"):
            if f.get("ext") == "mp4":
                return f["filesize"]
            if best is None:
                best = f
    if best:
        return best.get("filesize")
    return None

def ytdlp_download(url: str) -> str:
    job_id  = f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    outtmpl = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "mp4/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "continuedl": False,
        "nopart": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info     = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
    return filename

# ──────────────────────────────────────────────────────────────
# QUEUE SYSTEM (unified for Telegram + Web)
# ──────────────────────────────────────────────────────────────
@dataclass
class Job:
    job_id:    str
    user_key:  str              # user_id (tg) или IP (web)
    url:       str
    source:    str              # "telegram" | "web"
    chat_id:   Optional[int] = None   # только для telegram
    status:    str = "queued"   # queued|processing|done|error|toobig
    filename:  Optional[str] = None
    error_msg: str = ""
    size_mb:   float = 0.0
    created_at: float = field(default_factory=time.time)

job_store: Dict[str, Job] = {}
queue: asyncio.Queue = asyncio.Queue()
pending_per_key: Dict[str, int] = {}
last_request_time: Dict[str, float] = {}
queue_lock = asyncio.Lock()

async def enqueue(job: Job) -> bool:
    async with queue_lock:
        if queue.qsize() >= GLOBAL_QUEUE_LIMIT:
            return False
        if pending_per_key.get(job.user_key, 0) >= MAX_QUEUE_PER_USER:
            return False
        pending_per_key[job.user_key] = pending_per_key.get(job.user_key, 0) + 1
        job_store[job.job_id] = job
        await queue.put(job)
        return True

async def finish(user_key: str):
    async with queue_lock:
        pending_per_key[user_key] = max(0, pending_per_key.get(user_key, 1) - 1)

# ──────────────────────────────────────────────────────────────
# TELEGRAM API HELPER
# ──────────────────────────────────────────────────────────────
async def tg_send(chat_id: int, text: str):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

async def tg_send_video(chat_id: int, filepath: str) -> Optional[str]:
    """Upload video, return file_id."""
    if not BOT_TOKEN:
        return None
    async with httpx.AsyncClient(timeout=120) as client:
        with open(filepath, "rb") as f:
            r = await client.post(f"{TG_API}/sendVideo", data={"chat_id": str(chat_id)}, files={"video": f})
        data = r.json()
        if data.get("ok"):
            return data["result"]["video"]["file_id"]
    return None

# ──────────────────────────────────────────────────────────────
# WORKER
# ──────────────────────────────────────────────────────────────
async def worker():
    while True:
        job: Job = await queue.get()
        try:
            job.status = "processing"
            key = url_key(job.url)

            # Cache hit
            if key in url_cache and os.path.exists(url_cache[key]):
                job.status   = "done"
                job.filename  = url_cache[key]
                stats["served_from_cache"] = stats.get("served_from_cache", 0) + 1
                save_json(STATS_FILE, stats)
                if job.source == "telegram" and job.chat_id:
                    await tg_send_video(job.chat_id, job.filename)
                    if AD_TEXT:
                        await tg_send(job.chat_id, AD_TEXT)
                    await tg_send(job.chat_id, "✅ Готово (из кэша)!")
                add_history(job.url, "cache", job.size_mb, job.source)
                continue
            elif key in url_cache:
                url_cache.pop(key, None)
                save_json(CACHE_FILE, url_cache)

            if job.source == "telegram" and job.chat_id:
                await tg_send(job.chat_id, "⏳ Начинаю обработку...")

            # Probe size
            try:
                info = await asyncio.to_thread(ytdlp_probe, job.url)
                size = estimate_size_bytes(info)
                if size and size > MAX_BYTES:
                    stats["blocked_big"] = stats.get("blocked_big", 0) + 1
                    save_json(STATS_FILE, stats)
                    job.status  = "toobig"
                    job.size_mb = size / 1024 / 1024
                    if job.source == "telegram" and job.chat_id:
                        await tg_send(job.chat_id, f"🚫 Слишком большое видео (~{job.size_mb:.1f} МБ). Лимит {MAX_MB} МБ.")
                    add_history(job.url, "toobig", job.size_mb, job.source)
                    continue
            except Exception:
                pass  # probe failed — attempt download anyway

            # Download
            filepath = await asyncio.to_thread(ytdlp_download, job.url)

            real_size = None
            try:
                real_size = os.path.getsize(filepath)
            except Exception:
                pass

            if real_size and real_size > MAX_BYTES:
                stats["blocked_big"] = stats.get("blocked_big", 0) + 1
                save_json(STATS_FILE, stats)
                safe_cleanup(os.path.splitext(filepath)[0])
                job.status  = "toobig"
                job.size_mb = real_size / 1024 / 1024
                if job.source == "telegram" and job.chat_id:
                    await tg_send(job.chat_id, f"🚫 Слишком большое ({job.size_mb:.1f} МБ). Лимит {MAX_MB} МБ.")
                add_history(job.url, "toobig", job.size_mb, job.source)
                continue

            # Cache
            url_cache[key] = filepath
            save_json(CACHE_FILE, url_cache)

            job.status   = "done"
            job.filename  = filepath
            job.size_mb   = (real_size or 0) / 1024 / 1024

            stats["downloads_ok"] = stats.get("downloads_ok", 0) + 1
            save_json(STATS_FILE, stats)
            add_history(job.url, "ok", job.size_mb, job.source)

            if job.source == "telegram" and job.chat_id:
                await tg_send_video(job.chat_id, filepath)
                if AD_TEXT:
                    await tg_send(job.chat_id, AD_TEXT)
                await tg_send(job.chat_id, "✅ Готово!")

        except Exception as e:
            stats["errors"] = stats.get("errors", 0) + 1
            save_json(STATS_FILE, stats)
            job.status    = "error"
            job.error_msg = str(e)[:300]
            add_history(job.url, "error", 0, job.source)
            if job.source == "telegram" and job.chat_id:
                try:
                    await tg_send(job.chat_id, "❌ Ошибка при скачивании. Попробуй другую ссылку.")
                except Exception:
                    pass
        finally:
            await finish(job.user_key)
            queue.task_done()

# ──────────────────────────────────────────────────────────────
# FASTAPI APP
# ──────────────────────────────────────────────────────────────
app = FastAPI(title="SaveIt", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    asyncio.create_task(worker())
    # Auto-cleanup old jobs and files every hour
    async def _cleanup():
        while True:
            await asyncio.sleep(3600)
            now = time.time()
            for jid, job in list(job_store.items()):
                if now - job.created_at > 7200:
                    job_store.pop(jid, None)
    asyncio.create_task(_cleanup())

    # Register webhook if configured
    if BOT_TOKEN and WEBHOOK_URL:
        async with httpx.AsyncClient() as client:
            wh_url = WEBHOOK_URL.rstrip("/") + "/webhook"
            payload = {"url": wh_url}
            if WEBHOOK_SECRET:
                payload["secret_token"] = WEBHOOK_SECRET
            r = await client.post(f"{TG_API}/setWebhook", json=payload)
            print(f"Webhook set: {r.json()}")
    print("Worker started. App ready.")

# ──────────────────────────────────────────────────────────────
# TELEGRAM WEBHOOK ENDPOINT
# ──────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    # Verify secret if configured
    if WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret")

    update = await request.json()

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    user    = msg.get("from", {})
    user_id = user.get("id", 0)
    chat_id = msg.get("chat", {}).get("id", user_id)
    text    = msg.get("text", "")

    # Track users
    if user_id and user_id not in users:
        users.add(user_id)
        save_users()

    # Ban check
    if user_id in banned_ids:
        return {"ok": True}

    # Commands
    if text.startswith("/start"):
        await tg_send(chat_id,
            "📥 Инструкция: кинь ссылку — получи видео.\n"
            f"⚙️ Лимит: {MAX_MB} МБ\n"
            "🤖 Pinterest, Instagram, TikTok, YouTube и другие.\n"
            "🌐 Также доступна веб-версия!"
        )
        return {"ok": True}

    if text.startswith("/stats") and user_id in ADMIN_IDS:
        total = stats.get("total_requests", 0)
        err   = stats.get("errors", 0)
        succ  = 100 - (err * 100 // total) if total else 100
        await tg_send(chat_id,
            f"📊 Статистика\n"
            f"👥 Пользователи: {len(users)}\n"
            f"📥 Запросов: {total}\n"
            f"✅ Скачано: {stats.get('downloads_ok', 0)}\n"
            f"⚡ Из кэша: {stats.get('served_from_cache', 0)}\n"
            f"🚫 Большие: {stats.get('blocked_big', 0)}\n"
            f"❌ Ошибки: {err}\n"
            f"🔥 Успешность: {succ}%\n"
            f"🧠 Очередь: {queue.qsize()}\n"
            f"🔨 Забанено: {len(banned_ids)}"
        )
        return {"ok": True}

    if text.startswith("/ban ") and user_id in ADMIN_IDS:
        parts = text.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            target = int(parts[1])
            banned_ids.add(target)
            save_json(BANS_FILE, sorted(list(banned_ids)))
            await tg_send(chat_id, f"✅ Забанен: {target}")
        return {"ok": True}

    if text.startswith("/unban ") and user_id in ADMIN_IDS:
        parts = text.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            target = int(parts[1])
            banned_ids.discard(target)
            save_json(BANS_FILE, sorted(list(banned_ids)))
            await tg_send(chat_id, f"✅ Разбанен: {target}")
        return {"ok": True}

    # URL handling
    url = extract_url(text)
    if not url:
        await tg_send(chat_id, "Кинь ссылку одним сообщением 🙂")
        return {"ok": True}

    now  = time.time()
    last = last_request_time.get(str(user_id), 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        await tg_send(chat_id, f"⏳ Подожди {wait}с")
        return {"ok": True}
    last_request_time[str(user_id)] = now

    stats["total_requests"] = stats.get("total_requests", 0) + 1
    save_json(STATS_FILE, stats)

    job = Job(job_id=uuid.uuid4().hex, user_key=str(user_id), url=url, source="telegram", chat_id=chat_id)
    ok  = await enqueue(job)
    if not ok:
        await tg_send(chat_id, "🚫 Очередь перегружена или у тебя уже много запросов. Подожди 🙂")
        return {"ok": True}

    await tg_send(chat_id, f"✅ В очереди (позиция ~{queue.qsize()})")
    return {"ok": True}

# ──────────────────────────────────────────────────────────────
# WEB API
# ──────────────────────────────────────────────────────────────
class DownloadReq(BaseModel):
    url: str

class AdminReq(BaseModel):
    password: str

class BanReq(BaseModel):
    password: str
    target: int   # user_id or pseudo-id

def check_admin(password: str):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")

@app.post("/api/download")
async def api_download(req: DownloadReq, request: Request):
    ip = request.client.host

    url = extract_url(req.url)
    if not url:
        raise HTTPException(status_code=400, detail="Не найдена ссылка")

    now  = time.time()
    last = last_request_time.get(f"web_{ip}", 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        raise HTTPException(status_code=429, detail=f"Подожди {wait} секунд")
    last_request_time[f"web_{ip}"] = now

    stats["total_requests"] = stats.get("total_requests", 0) + 1
    save_json(STATS_FILE, stats)

    job = Job(job_id=uuid.uuid4().hex, user_key=f"web_{ip}", url=url, source="web")
    ok  = await enqueue(job)
    if not ok:
        raise HTTPException(status_code=429, detail="Очередь перегружена или слишком много запросов")

    return {"job_id": job.job_id, "queue_pos": queue.qsize()}

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    resp: Dict[str, Any] = {
        "status":    job.status,
        "error_msg": job.error_msg,
        "size_mb":   round(job.size_mb, 1),
        "ad_text":   AD_TEXT if job.status == "done" else "",
    }
    if job.status == "done":
        resp["download_url"] = f"/api/file/{job_id}"
    return resp

@app.get("/api/file/{job_id}")
async def api_file(job_id: str):
    job = job_store.get(job_id)
    if not job or job.status != "done" or not job.filename:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not os.path.exists(job.filename):
        raise HTTPException(status_code=410, detail="Файл удалён (истёк срок хранения)")
    fname = os.path.basename(job.filename)
    return FileResponse(job.filename, media_type="video/mp4", filename=fname,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

# --- Admin API ---
@app.post("/api/admin/stats")
async def admin_stats(req: AdminReq):
    check_admin(req.password)
    return {
        **stats,
        "queue_size":   queue.qsize(),
        "banned_count": len(banned_ids),
        "user_count":   len(users),
        "active_jobs":  len(job_store),
    }

@app.post("/api/admin/history")
async def admin_history(req: AdminReq):
    check_admin(req.password)
    return {"history": history[:100]}

@app.post("/api/admin/ban")
async def admin_ban(req: BanReq):
    check_admin(req.password)
    banned_ids.add(req.target)
    save_json(BANS_FILE, sorted(list(banned_ids)))
    return {"ok": True}

@app.post("/api/admin/unban")
async def admin_unban(req: BanReq):
    check_admin(req.password)
    banned_ids.discard(req.target)
    save_json(BANS_FILE, sorted(list(banned_ids)))
    return {"ok": True}

@app.get("/api/admin/banned")
async def admin_list_banned(password: str):
    check_admin(password)
    return {"banned": sorted(list(banned_ids))}

# Serve frontend
@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    html_path = Path("index.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

