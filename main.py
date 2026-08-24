import os
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

app = Application.builder().token(TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❤️ Добро пожаловать в OLIVKA MATCH!\n\n"
        "Бот работает ✅\n"
        "Скоро здесь будут анкеты, лайки и совпадения."
    )


app.add_handler(CommandHandler("start", start))


async def health(request):
    return web.Response(text="OLIVKA MATCH is running")


async def telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")


async def on_startup(web_app):
    await app.initialize()
    await app.start()

    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if render_url:
        await app.bot.set_webhook(f"{render_url}/telegram")


async def on_cleanup(web_app):
    await app.stop()
    await app.shutdown()


web_app = web.Application()
web_app.router.add_get("/", health)
web_app.router.add_post("/telegram", telegram_webhook)

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

web.run_app(web_app, host="0.0.0.0", port=PORT)
