
import asyncio
import logging
import os
import secrets
import asyncpg

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = "/webhook"

db_pool = None
bot_username = None

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN")

if not WEBHOOK_SECRET:
    raise ValueError("Не найден WEBHOOK_SECRET")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# Состояния
class SendMessageState(StatesGroup):
    waiting_for_text = State()
    waiting_for_reply = State()


# Главное меню
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✉️ Отправить сообщение")],
        [KeyboardButton(text="💬 Мои диалоги")],
        [KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
)

# Меню после перехода по ссылке
anonymous_message_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✉️ Написать анонимно")],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)


# START
@dp.message(CommandStart())
async def start_handler(
    message: Message,
    command: CommandObject,
    state: FSMContext
):
    user = message.from_user
    ref_code = command.args

    await state.clear()

    # Переход по персональной ссылке
    if ref_code:
        async with db_pool.acquire() as connection:
            recipient = await connection.fetchrow(
                """
                SELECT telegram_id
                FROM users
                WHERE ref_code = $1
                """,
                ref_code
            )

        if not recipient:
            await message.answer(
                "❌ Эта ссылка недействительна или устарела."
            )
            return

        if recipient["telegram_id"] == user.id:
            await message.answer(
                "Это твоя собственная ссылка. "
                "Отправлять сообщения самому себе нельзя."
            )
            return

        await state.update_data(
            recipient_id=recipient["telegram_id"]
        )

        await message.answer(
            "✉️ Ты перешёл по персональной ссылке ANON!\n\n"
            "Нажми кнопку ниже, чтобы написать владельцу ссылки.",
            reply_markup=anonymous_message_menu
        )
        return

    # Обычная регистрация
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


# Начало написания анонимного сообщения
@dp.message(lambda message: message.text == "✉️ Написать анонимно")
async def begin_anonymous_message(
    message: Message,
    state: FSMContext
):
    data = await state.get_data()

    if not data.get("recipient_id"):
        await message.answer(
            "Сначала перейди по персональной ссылке ANON."
        )
        return

    await state.set_state(SendMessageState.waiting_for_text)

    await message.answer(
        "✍️ Напиши текст анонимного сообщения:"
    )



# Получение и доставка анонимного сообщения
@dp.message(SendMessageState.waiting_for_text)
async def receive_anonymous_message(
    message: Message,
    state: FSMContext
):
    if message.text == "⬅️ Главное меню":
        await state.clear()
        await message.answer(
            "Главное меню ANON.",
            reply_markup=main_menu
        )
        return

    if not message.text:
        await message.answer(
            "Пожалуйста, отправь текстовое сообщение."
        )
        return

    data = await state.get_data()
    recipient_id = data.get("recipient_id")

    if not recipient_id:
        await state.clear()
        await message.answer(
            "Ссылка потерялась. Перейди по персональной ссылке ANON ещё раз.",
            reply_markup=main_menu
        )
        return

    sender_id = message.from_user.id
    user1_id = min(sender_id, recipient_id)
    user2_id = max(sender_id, recipient_id)

    async with db_pool.acquire() as connection:
        async with connection.transaction():
            dialog_id = await connection.fetchval(
                """
                INSERT INTO dialogs (user1_id, user2_id)
                VALUES ($1, $2)
                ON CONFLICT (user1_id, user2_id)
                WHERE is_active = TRUE
                DO NOTHING
                RETURNING id
                """,
                user1_id,
                user2_id
            )

            if dialog_id is None:
                dialog_id = await connection.fetchval(
                    """
                    SELECT id
                    FROM dialogs
                    WHERE user1_id = $1
                      AND user2_id = $2
                      AND is_active = TRUE
                    """,
                    user1_id,
                    user2_id
                )

            message_id = await connection.fetchval(
                """
                INSERT INTO messages (
                    dialog_id,
                    sender_id,
                    recipient_id,
                    text
                )
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                dialog_id,
                sender_id,
                recipient_id,
                message.text
            )

    try:
        await bot.send_message(
            chat_id=recipient_id,
            text=(
                "✉️ Тебе пришло новое анонимное сообщение!\n\n"
                f"{message.text}"
            )
        )

        async with db_pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE messages
                SET is_delivered = TRUE
                WHERE id = $1
                """,
                message_id
            )

        await message.answer("📨 Анонимное сообщение успешно доставлено!")

    except Exception:
        logging.exception("Ошибка доставки сообщения")

        await message.answer(
            "⚠️ Сообщение сохранено, но доставить его пока не удалось."
        )

    await state.clear()



# Возврат в главное меню
@dp.message(lambda message: message.text == "⬅️ Главное меню")
async def back_to_main_menu(
    message: Message,
    state: FSMContext
):
    await state.clear()
    await message.answer(
        "Главное меню ANON.",
        reply_markup=main_menu
    )


# Мои диалоги — список переписок
@dp.message(lambda message: message.text == "💬 Мои диалоги")
async def my_dialogs_handler(message: Message):
    user_id = message.from_user.id

    async with db_pool.acquire() as connection:
        dialogs = await connection.fetch(
            """
            SELECT
                id,
                CASE
                    WHEN user1_id = $1 THEN user2_id
                    ELSE user1_id
                END AS partner_id,
                created_at,
                is_active
            FROM dialogs
            WHERE user1_id = $1 OR user2_id = $1
            ORDER BY created_at DESC
            """,
            user_id
        )

    if not dialogs:
        await message.answer(
            "💬 У тебя пока нет диалогов.",
            reply_markup=main_menu
        )
        return

    await message.answer(
        f"💬 Твои диалоги: {len(dialogs)}"
    )

    for dialog in dialogs:
        status = "🟢 Активный" if dialog["is_active"] else "🔒 Завершённый"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть диалог",
                        callback_data=f"open_dialog:{dialog['id']}"
                    )
                ]
            ]
        )

        await message.answer(
            f"Диалог №{dialog['id']}\n"
            f"{status}",
            reply_markup=keyboard
        )

# Открытие конкретного диалога
@dp.callback_query(lambda callback: callback.data.startswith("open_dialog:"))
async def open_dialog_handler(callback: CallbackQuery):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow(
            """
            SELECT id, is_active
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
            """,
            dialog_id,
            user_id
        )

        if not dialog:
            await callback.answer(
                "Диалог не найден.",
                show_alert=True
            )
            return

        messages = await connection.fetch(
            """
            SELECT sender_id, text, created_at
            FROM messages
            WHERE dialog_id = $1
            ORDER BY created_at ASC
            LIMIT 50
            """,
            dialog_id
        )

    await callback.answer()

    if not messages:
        await callback.message.answer(
            "В этом диалоге пока нет сообщений."
        )
        return

    buttons = []

    if dialog["is_active"]:
        buttons.append([
            InlineKeyboardButton(
                text="↩️ Ответить",
                callback_data=f"reply_dialog:{dialog_id}"
            )
        ])

        buttons.append([
            InlineKeyboardButton(
                text="🚫 Заблокировать",
                callback_data=f"block_user:{dialog_id}"
            )
        ])

        buttons.append([
            InlineKeyboardButton(
                text="🔒 Завершить диалог",
                callback_data=f"finish_dialog:{dialog_id}"
            )
        ])

    reply_keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons
    )

    await callback.message.answer(
        f"💬 История диалога №{dialog_id}",
        reply_markup=reply_keyboard
    )

    for msg in messages:
        date = msg["created_at"].strftime("%d.%m.%Y %H:%M")

        if msg["sender_id"] == user_id:
            author = "Ты"
        else:
            author = "Собеседник"

        await callback.message.answer(
            f"{author}\n"
            f"🕒 {date}\n\n"
            f"{msg['text']}"
        )

# Запрос подтверждения завершения диалога
@dp.callback_query(lambda callback: callback.data.startswith("finish_dialog:"))
async def confirm_finish_dialog(callback: CallbackQuery):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow(
            """
            SELECT id
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
              AND is_active = TRUE
            """,
            dialog_id,
            user_id
        )

    if not dialog:
        await callback.answer(
            "Диалог уже завершён или недоступен.",
            show_alert=True
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, завершить",
                    callback_data=f"confirm_finish:{dialog_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"open_dialog:{dialog_id}"
                )
            ]
        ]
    )

    await callback.answer()

    await callback.message.answer(
        "🔒 Ты действительно хочешь завершить этот диалог?\n\n"
        "После завершения вы оба сможете читать историю, "
        "но отправлять новые сообщения уже не получится.",
        reply_markup=keyboard
    )

@dp.callback_query(lambda callback: callback.data.startswith("block_user:"))
async def block_user_handler(callback: CallbackQuery):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow("""
            SELECT user1_id, user2_id
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
        """, dialog_id, user_id)

    if not dialog:
        await callback.answer("Диалог не найден.", show_alert=True)
        return

    blocked_id = (
        dialog["user2_id"]
        if dialog["user1_id"] == user_id
        else dialog["user1_id"]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚫 Да, заблокировать",
                callback_data=f"confirm_block:{dialog_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Отмена",
                callback_data=f"open_dialog:{dialog_id}"
            )
        ]
    ])

    await callback.message.answer(
        "Заблокировать этого пользователя?\n\n"
        "Он не сможет отправлять тебе новые сообщения.",
        reply_markup=keyboard
    )

    await callback.answer()

@dp.callback_query(lambda callback: callback.data.startswith("confirm_block:"))
async def confirm_block_handler(callback: CallbackQuery):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow("""
            SELECT user1_id, user2_id
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
        """, dialog_id, user_id)

        if not dialog:
            await callback.answer("Диалог не найден.", show_alert=True)
            return

        blocked_id = (
            dialog["user2_id"]
            if dialog["user1_id"] == user_id
            else dialog["user1_id"]
        )

        await connection.execute("""
            INSERT INTO user_blocks (blocker_id, blocked_id)
            VALUES ($1, $2)
            ON CONFLICT (blocker_id, blocked_id) DO NOTHING
        """, user_id, blocked_id)

        await connection.execute("""
            UPDATE dialogs
            SET is_active = FALSE
            WHERE id = $1
        """, dialog_id)

    await callback.message.answer(
        "Пользователь заблокирован. Диалог завершён."
    )

    await callback.answer("Блокировка установлена.")

# Подтверждение завершения диалога
@dp.callback_query(lambda callback: callback.data.startswith("confirm_finish:"))
async def finish_dialog_handler(callback: CallbackQuery):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE dialogs
            SET is_active = FALSE
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
              AND is_active = TRUE
            """,
            dialog_id,
            user_id
        )

    if result == "UPDATE 0":
        await callback.answer(
            "Диалог уже завершён или недоступен.",
            show_alert=True
        )
        return

    await callback.answer("Диалог завершён!")

    await callback.message.edit_text(
        f"🔒 Диалог №{dialog_id} завершён.\n\n"
        "История переписки сохранена. Новые сообщения отправлять нельзя."
    )

# Начало ответа в существующем диалоге
@dp.callback_query(lambda callback: callback.data.startswith("reply_dialog:"))
async def begin_dialog_reply(
    callback: CallbackQuery,
    state: FSMContext
):
    dialog_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow(
            """
            SELECT id
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
              AND is_active = TRUE
            """,
            dialog_id,
            user_id
        )

    if not dialog:
        await callback.answer(
            "Диалог недоступен или уже завершён.",
            show_alert=True
        )
        return

    await state.update_data(reply_dialog_id=dialog_id)
    await state.set_state(SendMessageState.waiting_for_reply)

    await callback.answer()

    await callback.message.answer(
        "✍️ Напиши текст ответа. Он будет отправлен собеседнику."
    )

# Получение и доставка ответа в диалоге
@dp.message(SendMessageState.waiting_for_reply)
async def receive_dialog_reply(
    message: Message,
    state: FSMContext
):
    if message.text == "⬅️ Главное меню":
        await state.clear()
        await message.answer(
            "Главное меню ANON.",
            reply_markup=main_menu
        )
        return

    if not message.text:
        await message.answer(
            "Пожалуйста, отправь текстовое сообщение."
        )
        return

    data = await state.get_data()
    dialog_id = data.get("reply_dialog_id")
    sender_id = message.from_user.id

    if not dialog_id:
        await state.clear()
        await message.answer(
            "Диалог не найден. Открой его заново.",
            reply_markup=main_menu
        )
        return

    async with db_pool.acquire() as connection:
        dialog = await connection.fetchrow(
            """
            SELECT user1_id, user2_id
            FROM dialogs
            WHERE id = $1
              AND (user1_id = $2 OR user2_id = $2)
              AND is_active = TRUE
            """,
            dialog_id,
            sender_id
        )

        if not dialog:
            await state.clear()
            await message.answer(
                "Диалог недоступен или уже завершён.",
                reply_markup=main_menu
            )
            return

        recipient_id = (
            dialog["user2_id"]
            if dialog["user1_id"] == sender_id
            else dialog["user1_id"]
        )

        message_id = await connection.fetchval(
            """
            INSERT INTO messages (
                dialog_id,
                sender_id,
                recipient_id,
                text
            )
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            dialog_id,
            sender_id,
            recipient_id,
            message.text
        )

    try:
        await bot.send_message(
            chat_id=recipient_id,
            text=(
                "💬 Новое сообщение в ANON!\n\n"
                f"{message.text}"
            )
        )

        async with db_pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE messages
                SET is_delivered = TRUE
                WHERE id = $1
                """,
                message_id
            )

        await message.answer(
            "📨 Ответ успешно доставлен!"
        )

    except Exception:
        logging.exception("Ошибка доставки ответа")
        await message.answer(
            "⚠️ Ответ сохранён, но доставить его пока не удалось."
        )

    await state.clear()


# Профиль
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
        await message.answer(
            "Профиль не найден. Напиши /start."
        )
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


# Запуск webhook
async def on_startup(bot: Bot):
    render_url = os.getenv("RENDER_EXTERNAL_URL")

    if not render_url:
        raise ValueError("Не найден RENDER_EXTERNAL_URL")

    global bot_username

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    await bot.set_webhook(
        url=f"{render_url}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
    )

    logging.info("ANON webhook установлен")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    await bot.session.close()


# Основной запуск
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
    async with db_pool.acquire() as connection:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL,
                recipient_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                is_delivered BOOLEAN DEFAULT FALSE
            )
        """)

    logging.info("Таблица сообщений готова")

    async with db_pool.acquire() as connection:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS dialogs (
                id BIGSERIAL PRIMARY KEY,
                user1_id BIGINT NOT NULL,
                user2_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE,
                UNIQUE (user1_id, user2_id)
            )
        """)

    logging.info("Таблица диалогов готова")

    async with db_pool.acquire() as connection:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS user_blocks (
                id BIGSERIAL PRIMARY KEY,
                blocker_id BIGINT NOT NULL,
                blocked_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (blocker_id, blocked_id),
                CHECK (blocker_id != blocked_id)
            )
        """)

    logging.info("Таблица блокировок готова")

    async with db_pool.acquire() as connection:
        await connection.execute("""
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS dialog_id BIGINT
            REFERENCES dialogs(id)
        """)

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
        await site.start()
        logging.info("ANON запущен на порту %s", port)

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
