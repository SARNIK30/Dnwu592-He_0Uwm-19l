import os
import re
import time
import uuid
import glob
import yt_dlp

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return re.sub(r"[)\]}>,.]+$", "", m.group(1))


def safe_cleanup(prefix: str):
    # удаляем все файлы, которые начинаются с prefix (mp4/webm/m4a и т.д.)
    for p in glob.glob(prefix + ".*"):
        try:
            os.remove(p)
        except Exception:
            pass


def download_video(url: str) -> str:
    # Уникальный префикс на каждое скачивание
    job_id = f"dl_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    outtmpl = f"{job_id}.%(ext)s"

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "mp4/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # важно: не использовать старые куски/кэш
        "overwrites": True,
        "continuedl": False,

        # чтобы не прилипал старый файл
        "nopart": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    # yt-dlp иногда отдаёт имя не mp4 (например webm) — это ок
    return filename


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("Кинь ссылку одним сообщением 🙂")
        return

    await update.message.reply_text("⏳ Скачиваю...")

    file_path = None
    try:
        file_path = download_video(url)

        # Отправляем именно тот файл, который скачали
        with open(file_path, "rb") as f:
            await update.message.reply_video(video=f)

        await update.message.reply_text("✅ Готово!")

    except Exception as e:
        print("DOWNLOAD ERROR:", e)
        await update.message.reply_text("❌ Не удалось скачать (возможно ссылка/сервис ограничен).")

    finally:
        # Удаляем скачанный файл, чтобы не залипало на прошлом
        if file_path:
            prefix = os.path.splitext(file_path)[0]
            safe_cleanup(prefix)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
