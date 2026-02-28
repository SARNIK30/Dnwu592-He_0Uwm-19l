import os
import re
import json
import time
import uuid
import glob
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, Set

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------- CONFIG -----------------
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = set(
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip().isdigit()
)

MAX_MB = 50
MAX_BYTES = MAX_MB * 1024 * 1024

# антиспам
COOLDOWN_SECONDS = 12          # минимум секунд между запросами от одного юзера
MAX_QUEUE_PER_USER = 2         # максимум задач в очереди от одного юзера
GLOBAL_QUEUE_LIMIT = 100       # чтобы не убили бот

# реклама/сообщение можно включить позже
AD_TEXT = os.getenv("AD_TEXT", "").strip()  # пусто = без рекламы

# файлы хранения
CACHE_FILE = "cache.json"      # url_key -> telegram file_id
BANS_FILE = "bans.json"        # banned user ids
STATS_FILE = "stats.json"      # простые счётчики

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

# ----------------- STORAGE -----------------
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

cache: Dict[str, str] = load_json(CACHE_FILE, {})
banned: Set[int] = set(load_json(BANS_FILE, []))
stats: Dict[str, Any] = load_json(STATS_FILE, {
    "total_requests": 0,
    "served_from_cache": 0,
    "downloads_ok": 0,
    "blocked_big": 0,
    "errors": 0,
})

# ----------------- HELPERS -----------------
def extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(1)
    return re.sub(r"[)\]}>,.]+$", "", url)

def url_key(url: str) -> str:
    # ключ для кэша. Можно усложнить позже (по id видео), но это уже работает.
    return url.strip()

def safe_cleanup(prefix: str):
    for p in glob.glob(prefix + ".*"):
        try:
            os.remove(p)
        except Exception:
            pass

def ytdlp_probe(url: str) -> Dict[str, Any]:
    """
    Получаем метаданные БЕЗ скачивания, чтобы оценить размер.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info

def estimate_size_bytes(info: Dict[str, Any]) -> Optional[int]:
    """
    Пытаемся оценить размер файла.
    """
    # прямой размер
    for k in ("filesize", "filesize_approx"):
        v = info.get(k)
        if isinstance(v, int) and v > 0:
            return v

    # иногда есть форматы
    fmts = info.get("formats") or []
    # попробуем выбрать mp4/best и взять filesize
    best = None
    for f in fmts:
        if f.get("filesize"):
            # предпочитаем mp4
            if f.get("ext") == "mp4":
                best = f
                break
            if best is None:
                best = f
    if best and isinstance(best.get("filesize"), int):
        return best["filesize"]

    return None

def ytdlp_download(url: str) -> str:
    """
    Скачиваем в уникальный файл, чтобы не было залипания.
    Возвращаем путь.
    """
    job_id = f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    outtmpl = f"{job_id}.%(ext)s"

    ydl_opts = {
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return filename

# ----------------- QUEUE SYSTEM -----------------
@dataclass
class Job:
    chat_id: int
    user_id: int
    url: str
    message_id: int

queue: asyncio.Queue[Job] = asyncio.Queue()
pending_per_user: Dict[int, int] = {}
last_request_time: Dict[int, float] = {}
queue_lock = asyncio.Lock()

async def enqueue_job(job: Job) -> bool:
    async with queue_lock:
        if queue.qsize() >= GLOBAL_QUEUE_LIMIT:
            return False
        pending = pending_per_user.get(job.user_id, 0)
        if pending >= MAX_QUEUE_PER_USER:
            return False
        pending_per_user[job.user_id] = pending + 1
        await queue.put(job)
        return True

async def finish_job(user_id: int):
    async with queue_lock:
        pending_per_user[user_id] = max(0, pending_per_user.get(user_id, 1) - 1)

# ----------------- BOT HANDLERS -----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Инструкция: кидаешь ссылку и получаешь видео.\n"
        f"⚙️ Лимит: {MAX_MB} МБ\n"
        "🤖 Скачиваю из: Pinterest, Instagram, TikTok и т.д."
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")

    await update.message.reply_text(
        "🛠️ Админ-панель:\n"
        "/stats — статистика\n"
        "/ban <id> — бан\n"
        "/unban <id> — разбан\n"
        "/setad <текст> — установить рекламную строку\n"
        "/rofl — рофл-панель 😁"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")

    # ---- берём данные ----
    total = stats.get("total_requests", 0)
    downloads = stats.get("downloads_ok", 0)
    cache = stats.get("served_from_cache", 0)
    errors = stats.get("errors", 0)
    blocked = stats.get("blocked_big", 0)
    queue_size = queue.qsize()
    banned_count = len(banned)

    success = 100
    if total > 0:
        success = 100 - (errors * 100 // total)

    # ---- красивый текст ----
    txt = f"""
📊 <b>Pin Save Robot — Статистика</b>

👥 Запросов всего: <b>{total:,}</b>
📥 Успешных скачиваний: <b>{downloads:,}</b>
⚡ Отдано из кэша: <b>{cache:,}</b>

🚫 Большие файлы: <b>{blocked:,}</b>
❌ Ошибки: <b>{errors:,}</b>

🧠 Очередь: <b>{queue_size}</b>
🔨 Забанено: <b>{banned_count}</b>

🔥 Успешность: <b>{success}%</b>
"""

    await update.message.reply_text(txt, parse_mode="HTML")
    
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Использование: /ban <id>")

    target = int(context.args[0])
    banned.add(target)
    save_json(BANS_FILE, sorted(list(banned)))
    await update.message.reply_text(f"✅ Забанен: {target}")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Использование: /unban <id>")

    target = int(context.args[0])
    banned.discard(target)
    save_json(BANS_FILE, sorted(list(banned)))
    await update.message.reply_text(f"✅ Разбанен: {target}")

async def setad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AD_TEXT
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")

    text = update.message.text.replace("/setad", "", 1).strip()
    AD_TEXT = text
    await update.message.reply_text("✅ AD_TEXT обновлён.")

async def rofl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("⛔ Нет доступа.")

    phrases = [
        "😁 Рофл-панель активирована: админ теперь официально мем.",
        "🧠 IQ +100 за каждый /rofl",
        "🔥 Этот бот работает на чистом энтузиазме и очереди",
        "🗿 Админ: *смотрит логи* — ‘Ну да, ну да…’",
    ]
    await update.message.reply_text(phrases[int(time.time()) % len(phrases)])

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in banned:
        return

    url = extract_url(update.message.text or "")
    if not url:
        return await update.message.reply_text("Кинь ссылку одним сообщением 🙂")

    # антиспам: кулдаун
    now = time.time()
    last = last_request_time.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        return await update.message.reply_text(f"⏳ Не так быстро 🙂 подожди {wait}с")
    last_request_time[user_id] = now

    stats["total_requests"] = stats.get("total_requests", 0) + 1
    save_json(STATS_FILE, stats)

    key = url_key(url)
    if key in cache:
        # отдаём из кэша мгновенно
        try:
            await update.message.reply_video(cache[key])
            stats["served_from_cache"] = stats.get("served_from_cache", 0) + 1
            save_json(STATS_FILE, stats)
            if AD_TEXT:
                await update.message.reply_text(AD_TEXT)
            return
        except Exception:
            # если file_id протух/битый — удалим и пойдём в скачивание
            cache.pop(key, None)
            save_json(CACHE_FILE, cache)

    job = Job(chat_id=chat_id, user_id=user_id, url=url, message_id=update.message.message_id)
    ok = await enqueue_job(job)
    if not ok:
        return await update.message.reply_text(
            "🚫 Очередь перегружена или у тебя уже слишком много запросов.\n"
            "Подожди немного и попробуй снова 🙂"
        )

    pos = queue.qsize()
    await update.message.reply_text(f"✅ В очереди. Позиция примерно: {pos}")

# ----------------- WORKER -----------------
async def worker(app: Application):
    while True:
        job = await queue.get()
        try:
            await app.bot.send_message(job.chat_id, "⏳ Начинаю обработку...")

            # 1) probe размер
            info = None
            try:
                info = await asyncio.to_thread(ytdlp_probe, job.url)
            except Exception:
                info = None  # если probe не вышел — попробуем скачать, но можем отрубить по факту

            if info:
                size = estimate_size_bytes(info)
                if size and size > MAX_BYTES:
                    stats["blocked_big"] = stats.get("blocked_big", 0) + 1
                    save_json(STATS_FILE, stats)
                    await app.bot.send_message(
                        job.chat_id,
                        f"🚫 Слишком большое видео (~{size/1024/1024:.1f} МБ). Лимит {MAX_MB} МБ."
                    )
                    continue

            # 2) download
            file_path = await asyncio.to_thread(ytdlp_download, job.url)

            # 3) проверка размера по факту
            try:
                real_size = os.path.getsize(file_path)
            except Exception:
                real_size = None

            if real_size and real_size > MAX_BYTES:
                stats["blocked_big"] = stats.get("blocked_big", 0) + 1
                save_json(STATS_FILE, stats)
                await app.bot.send_message(
                    job.chat_id,
                    f"🚫 Слишком большое видео ({real_size/1024/1024:.1f} МБ). Лимит {MAX_MB} МБ."
                )
                # удалить
                prefix = os.path.splitext(file_path)[0]
                safe_cleanup(prefix)
                continue

            # 4) send to telegram (upload)
            with open(file_path, "rb") as f:
                msg = await app.bot.send_video(job.chat_id, video=f)

            # 5) cache file_id
            try:
                key = url_key(job.url)
                cache[key] = msg.video.file_id
                save_json(CACHE_FILE, cache)
            except Exception:
                pass

            stats["downloads_ok"] = stats.get("downloads_ok", 0) + 1
            save_json(STATS_FILE, stats)

            if AD_TEXT:
                await app.bot.send_message(job.chat_id, AD_TEXT)
            await app.bot.send_message(job.chat_id, "✅ Готово!")

            # 6) cleanup
            prefix = os.path.splitext(file_path)[0]
            safe_cleanup(prefix)

        except Exception as e:
            stats["errors"] = stats.get("errors", 0) + 1
            save_json(STATS_FILE, stats)
            try:
                await app.bot.send_message(job.chat_id, "❌ Ошибка обработки ссылки.")
            except Exception:
                pass
        finally:
            await finish_job(job.user_id)
            queue.task_done()

# ----------------- MAIN -----------------
async def on_startup(app: Application):
    # запускаем один воркер (очередь)
    asyncio.create_task(worker(app))
    print("Worker started")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = Application.builder().token(TOKEN).build()

    # user handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # admin handlers
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("setad", setad_cmd))
    app.add_handler(CommandHandler("rofl", rofl_cmd))

    app.post_init = on_startup

    print("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
