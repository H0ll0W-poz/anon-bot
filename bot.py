
import asyncio
import logging
import os

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "/webhook"

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN")

if not WEBHOOK_SECRET:
    raise ValueError("Не найден WEBHOOK_SECRET")

bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Добро пожаловать в ANON!\n\n"
        "Анонимные сообщения — просто, удобно и конфиденциально."
    )


async def on_startup(bot: Bot):
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not render_url:
        raise ValueError("Не найден RENDER_EXTERNAL_URL")

    await bot.set_webhook(
        url=f"{render_url}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
    )

    logging.info("ANON webhook установлен")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    await bot.session.close()




async def main():
    logging.basicConfig(level=logging.INFO)

    app = web.Application()

    async def health_check(request):
        return web.Response(text="ANON is running")

    app.router.add_get("/", health_check)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )

    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", "10000"))

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=port,
    )

    webhook_task = None

    try:
        # Сначала открываем порт для Render
        await site.start()
        logging.info("ANON запущен на порту %s", port)

        # Затем устанавливаем webhook
        async def install_webhook():
            try:
                await on_startup(bot)
            except Exception:
                logging.exception("Ошибка установки webhook")

        webhook_task = asyncio.create_task(install_webhook())

        await asyncio.Event().wait()

    finally:
        if webhook_task and not webhook_task.done():
            webhook_task.cancel()
            try:
                await webhook_task
            except asyncio.CancelledError:
                pass

        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
