import { Telegraf, Markup } from "telegraf";
import fetch from "node-fetch";

const bot = new Telegraf(process.env.BOT_TOKEN);

// ---- helpers ----
function extractUrl(text = "") {
  const m = text.match(/https?:\/\/\S+/i);
  return m ? m[0].replace(/[)\]}>,.]+$/g, "") : null;
}

function detectPlatform(url) {
  const u = url.toLowerCase();
  if (u.includes("pinterest.") || u.includes("pin.it")) return "pinterest";
  if (u.includes("instagram.com")) return "instagram";
  if (u.includes("tiktok.com")) return "tiktok";
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
  if (/\.(mp4|mov|webm|mp3|m4a)(\?|$)/i.test(u)) return "direct";
  return "unknown";
}

// ---- pinterest ----
async function pinterestGetVideoUrl(url) {
  const res = await fetch(url, {
    redirect: "follow",
    headers: {
      "user-agent":
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36",
    },
  });

  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const html = await res.text();

  const og1 = html.match(/property=["']og:video["']\s+content=["']([^"']+)["']/i);
  if (og1?.[1]) return og1[1];

  const og2 = html.match(/property=["']og:video:url["']\s+content=["']([^"']+)["']/i);
  if (og2?.[1]) return og2[1];

  throw new Error("Видео не найдено (пин может быть не видео или доступ ограничен).");
}

// ---- universal ----
async function sendDirect(ctx, url) {
  // Telegram часто умеет отправлять видео по URL напрямую
  await ctx.replyWithVideo(url);
}

// ---- bot ----
bot.start(async (ctx) => {
  await ctx.reply(
    "📥 Пришли ссылку.\n\n" +
      "✅ Скачиваю: Pinterest (публичные видео) и прямые ссылки на файлы (.mp4/.mov/.mp3)\n" +
      "ℹ️ Instagram/TikTok/YouTube — покажу кнопку открыть."
  );
});

bot.on("text", async (ctx) => {
  const url = extractUrl(ctx.message.text);
  if (!url) return ctx.reply("Кинь ссылку одним сообщением 🙂");

  const platform = detectPlatform(url);

  try {
    if (platform === "pinterest") {
      await ctx.reply("⏳ Ищу видео в Pinterest...");
      const videoUrl = await pinterestGetVideoUrl(url);
      await sendDirect(ctx, videoUrl);
      return ctx.reply("✅ Готово!");
    }

    if (platform === "direct") {
      await ctx.reply("⏳ Отправляю файл...");
      await sendDirect(ctx, url);
      return ctx.reply("✅ Готово!");
    }

    if (platform === "instagram" || platform === "tiktok" || platform === "youtube") {
      const pretty =
        platform === "instagram" ? "Instagram" : platform === "tiktok" ? "TikTok" : "YouTube";

      return ctx.reply(
        `ℹ️ Это ссылка ${pretty}.\nНажми кнопку ниже:`,
        Markup.inlineKeyboard([Markup.button.url(`Открыть в ${pretty}`, url)])
      );
    }

    return ctx.reply(
      "❌ Пока поддерживаю:\n• Pinterest видео\n• прямые ссылки на .mp4/.mov/.mp3\n• IG/TT/YT — кнопка “Открыть”"
    );
  } catch (e) {
    console.log(e);
    return ctx.reply("❌ Не получилось обработать ссылку. Попробуй другой пин/ссылку.");
  }
});

bot.launch();
console.log("Bot started");
