import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Привет! Я бот для управления персоналом в HoReCa")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан: скопируй .env.example в .env и впиши токен")

    bot = Bot(token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
