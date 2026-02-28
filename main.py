import os
import re
import tempfile
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(1)
    return re.sub(r"[)\]}>,.]+$", "", url)

def is_direct_file(url: str) -> bool:
    return re.search(r"\.(mp4|mov|webm)(\?|$)", url, re.IGNORECASE) is not None

def fetch_html(url: str) -> str:
    r = requests.get(
        url,
        allow_redirects=True,
        timeout=20,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile"
        },
    )
    r.raise_for_status()
    return r.text

def pinterest_extract_video_url(url: str) -> str:
    html = fetch_html(url)

    # 1) Пробуем og:video
    m = re.search(r'property=["\']og:video["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r'property=["\']og:video:url["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1)

    # 2) Иногда встречается прямой mp4 в html
    m = re.search(r'(https://[^"\']+\.mp4[^"\']*)', html, re.IGNORECASE)
    if m:
        return m.group(1)

    raise ValueError("Видео не найдено (пин может быть не видео или доступ ограничен).")

def download_to_tempfile(file_url: str) -> str:
    r = requests.get(
        file_url,
        stream=True,
        timeout=60,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile"
        },
    )
    r.raise_for_status()

    # временный файл
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)

    return path

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Пришли ссылку Pinterest (видео) или прямую ссылку на .mp4/.mov/.webm"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("Кинь ссылку одним сообщением 🙂")
        return

    try:
        # Pinterest
        if "pinterest." in url.lower() or "pin.it" in url.lower():
            await update.message.reply_text("⏳ Ищу видео в Pinterest...")
            video_url = pinterest_extract_video_url(url)

            await update.message.reply_text("⏳ Скачиваю файл...")
            tmp_path = download_to_tempfile(video_url)

            try:
                with open(tmp_path, "rb") as f:
                    await update.message.reply_video(video=f)
                await update.message.reply_text("✅ Готово!")
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return

        # Direct file
        if is_direct_file(url):
            await update.message.reply_text("⏳ Отправляю...")
            await update.message.reply_video(video=url)
            await update.message.reply_text("✅ Готово!")
            return

        await update.message.reply_text("❌ Пока поддерживаю Pinterest видео и прямые ссылки на .mp4/.mov/.webm")

    except Exception as e:
        # В проде можно логировать e
        await update.message.reply_text("❌ Не получилось скачать. Попробуй другой пин/ссылку.")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # polling для Render ок (не webhook)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
