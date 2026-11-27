
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Добро пожаловать в ANON!\n\n"
        "Анонимные сообщения — просто, удобно и конфиденциально."
    )


async def main():
    logging.basicConfig(level=logging.INFO)

    if not TOKEN:
        raise ValueError("Не найден BOT_TOKEN в файле .env")

    bot = Bot(token=TOKEN)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
