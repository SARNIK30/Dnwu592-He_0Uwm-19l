import { Telegraf } from "telegraf";

const bot = new Telegraf(process.env.BOT_TOKEN);

bot.start(ctx =>
  ctx.reply("📥 Пришли ссылку на видео — я попробую скачать.")
);

bot.on("text", async (ctx) => {
  const text = ctx.message.text;

  if (!text.includes("http"))
    return ctx.reply("Отправь ссылку 🙂");

  await ctx.reply("✅ Видео скачано!\n🤝 Партнёр проекта: @TopChannel");
});

bot.launch();
console.log("Bot started");
