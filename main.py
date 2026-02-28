import os
import yt_dlp
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")


def download_video(url):
    ydl_opts = {
        "format": "mp4",
        "outtmpl": "video.%(ext)s",
        "quiet": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    return filename


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text

    await update.message.reply_text("⏳ Скачиваю...")

    try:
        file_path = download_video(url)

        with open(file_path, "rb") as f:
            await update.message.reply_video(f)

        await update.message.reply_text("✅ Готово!")

    except Exception as e:
        print(e)
        await update.message.reply_text("❌ Не удалось скачать")


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
