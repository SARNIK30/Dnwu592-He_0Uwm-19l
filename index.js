import { Telegraf } from "telegraf";

const bot = new Telegraf(process.env.BOT_TOKEN);

bot.start((ctx) => {
  ctx.reply("✅ Бот работает. Отправь ссылку.");
});

bot.on("text", (ctx) => {
  ctx.reply("Я получил сообщение 👍");
});

bot.launch();

console.log("Bot started");
