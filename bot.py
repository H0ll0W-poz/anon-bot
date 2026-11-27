
import asyncio
import logging
import os
import secrets
import asyncpg

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "/webhook"
db_pool = None

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN")

if not WEBHOOK_SECRET:
    raise ValueError("Не найден WEBHOOK_SECRET")

bot = Bot(token=TOKEN)
bot_username = None
dp = Dispatcher()
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="✉️ Отправить сообщение"),
        ],
        [
            KeyboardButton(text="💬 Мои диалоги"),
        ],
        [
            KeyboardButton(text="👤 Мой профиль"),
        ],
        [
            KeyboardButton(text="⚙️ Настройки"),
        ],
        [
            KeyboardButton(text="❓ Помощь"),
        ],
    ],
    resize_keyboard=True,
)



@dp.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user

    async with db_pool.acquire() as connection:
        new_ref_code = secrets.token_urlsafe(9)

        result = await connection.fetchrow(
            """
            INSERT INTO users (telegram_id, username, ref_code)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                ref_code = COALESCE(users.ref_code, EXCLUDED.ref_code)
            RETURNING (xmax = 0) AS is_new, ref_code
            """,
            user.id,
            user.username,
            new_ref_code
        )

    @dp.message(lambda message: message.text == "👤 Мой профиль")
    async def profile_handler(message: Message):
        user = message.from_user

        async with db_pool.acquire() as connection:
            profile = await connection.fetchrow(
                """
                SELECT username, registered_at, ref_code
                FROM users
                WHERE telegram_id = $1
                """,
                user.id
            )

        if not profile:
            await message.answer("Профиль не найден. Напиши /start.")
            return

        username = (
            f"@{profile['username']}"
            if profile["username"]
            else "Не установлен"
        )

        profile_link = (
            f"https://t.me/{bot_username}?start={profile['ref_code']}"
        )

        await message.answer(
            "👤 Твой профиль ANON\n\n"
            f"Username: {username}\n"
            "Дата регистрации: приватная\n\n"
            "🔗 Твоя персональная ссылка:\n"
            f"{profile_link}"
        )

    if result["is_new"]:
        await message.answer(
            "👋 Добро пожаловать в ANON!\n\n"
            "Твоя регистрация успешно завершена.\n"
            "Теперь ты можешь пользоваться анонимными сообщениями.",
            reply_markup=main_menu
        )
    else:
        await message.answer(
            "С возвращением в ANON!\n\n"
            "Твой профиль найден.",
            reply_markup=main_menu
        )



async def on_startup(bot: Bot):
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not render_url:
        raise ValueError("Не найден RENDER_EXTERNAL_URL")

    await bot.set_webhook(
        url=f"{render_url}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
    )
    global bot_username
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    logging.info("ANON webhook установлен")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    await bot.session.close()




async def main():
    logging.basicConfig(level=logging.INFO)
    global db_pool

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("Не найден DATABASE_URL")

    db_pool = await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=5
    )
    async with db_pool.acquire() as connection:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                registered_at TIMESTAMPTZ DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE,
                ref_code TEXT UNIQUE
            )
        """)

        await connection.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS ref_code TEXT UNIQUE
        """)

    logging.info("Таблица пользователей готова")

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

        if db_pool:
            await db_pool.close()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
