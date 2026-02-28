import os
import re
import requests
import tempfile
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


def extract_url(text: str):
    m = URL_RE.search(text or "")
    if not m:
        return None
    return re.sub(r"[)\]}>,.]+$", "", m.group(1))


def is_direct(url: str):
    return re.search(r"\.(mp4|mov|webm)(\?|$)", url, re.IGNORECASE)


def pinterest_video(url: str):
    r = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14)"
        },
        timeout=20,
    )
    html = r.text

    m = re.search(r'(https://[^"]+\.mp4[^"]*)', html)
    if m:
        return m.group(1)

    raise Exception("video not found")


def download_temp(video_url: str):
    r = requests.get(video_url, stream=True)
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    with open(path, "wb") as f:
        for chunk in r.iter_content(1024 * 256):
            if chunk:
                f.write(chunk)

    return path


# -------- BOT --------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Пришли ссылку Pinterest или прямую .mp4"
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_url(update.message.text)

    if not url:
        await update.message.reply_text("Кинь ссылку 🙂")
        return

    try:
        # Pinterest
        if "pinterest" in url or "pin.it" in url:
            await update.message.reply_text("⏳ Ищу видео...")
            v = pinterest_video(url)

            await update.message.reply_text("⏳ Скачиваю...")
            path = download_temp(v)

            with open(path, "rb") as f:
                await update.message.reply_video(f)

            os.remove(path)
            await update.message.reply_text("✅ Готово!")
            return

        # direct file
        if is_direct(url):
            await update.message.reply_video(url)
            await update.message.reply_text("✅ Готово!")
            return

        await update.message.reply_text("❌ Пока только Pinterest и .mp4")

    except Exception as e:
        print(e)
        await update.message.reply_text("❌ Ошибка скачивания")


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
