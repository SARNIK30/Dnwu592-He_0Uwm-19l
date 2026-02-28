import { Telegraf } from "telegraf";
import fetch from "node-fetch";

const bot = new Telegraf(process.env.BOT_TOKEN);

function extractUrl(text = "") {
  const m = text.match(/https?:\/\/\S+/i);
  return m ? m[0].replace(/[)\]}>,.]+$/g, "") : null;
}

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

  // Ищем JSON с видео внутри страницы
  const jsonMatch = html.match(/"video_list":({.*?})/);

  if (jsonMatch?.[1]) {
    const videoData = JSON.parse(jsonMatch[1]);
    const firstKey = Object.keys(videoData)[0];
    if (firstKey && videoData[firstKey]?.url) {
      return videoData[firstKey].url;
    }
  }

  throw new Error("Видео не найдено в JSON Pinterest.");
}

  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const html = await res.text();

  const og1 = html.match(/property=["']og:video["']\s+content=["']([^"']+)["']/i);
  if (og1?.[1]) return og1[1];

  const og2 = html.match(/property=["']og:video:url["']\s+content=["']([^"']+)["']/i);
  if (og2?.[1]) return og2[1];

  throw new Error("Видео не найдено (это может быть не видео-пин или доступ ограничен).");
}

bot.start((ctx) => {
  ctx.reply("📥 Пришли ссылку Pinterest (видео) или прямую ссылку на .mp4");
});

bot.on("text", async (ctx) => {
  const url = extractUrl(ctx.message.text);
  if (!url) return ctx.reply("Кинь ссылку одним сообщением 🙂");

  try {
    // Pinterest
    if (url.includes("pinterest.") || url.includes("pin.it")) {
      await ctx.reply("⏳ Ищу видео в Pinterest...");
      const videoUrl = await pinterestGetVideoUrl(url);
      await ctx.replyWithVideo(videoUrl);
      return ctx.reply("✅ Готово!");
    }

    // direct mp4
    if (url.match(/\.(mp4|mov|webm)(\?|$)/i)) {
      await ctx.reply("⏳ Отправляю файл...");
      await ctx.replyWithVideo(url);
      return ctx.reply("✅ Готово!");
    }

    return ctx.reply("❌ Пока поддерживаю Pinterest видео и прямые ссылки на .mp4/.mov/.webm");
  } catch (e) {
    console.log(e);
    return ctx.reply("❌ Не получилось скачать. Попробуй другой пин/ссылку.");
  }
});

bot.launch();
console.log("Bot started");
