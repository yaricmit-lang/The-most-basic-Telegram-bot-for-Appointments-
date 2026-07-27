"""Точка входа: запуск бота и фонового цикла уведомлений."""
import asyncio
import fcntl
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

import config
import db
import scheduler
from handlers import admin as admin_handlers
from handlers import client as client_handlers


LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
_lock_handle = None


def acquire_single_instance():
    """Второй экземпляр бота = второй планировщик = дубли уведомлений. Не пускаем.

    Блокировка снимается сама, когда процесс завершается (даже при kill -9).
    """
    global _lock_handle
    # "a+", а не "w": режим "w" обрезал бы файл ещё до захвата, и неудачный
    # второй запуск стирал бы PID работающего бота.
    _lock_handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handle.seek(0)
    _lock_handle.truncate()
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True


async def setup_commands(bot: Bot):
    await bot.set_my_commands([BotCommand(command="start", description="Меню")])
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Меню"),
                    BotCommand(command="admin", description="Панель мастера"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            pass  # админ ещё не открывал чат с ботом


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен из @BotFather."
        )
    if not acquire_single_instance():
        raise SystemExit(
            "Бот уже запущен в другом процессе — второй экземпляр слал бы дубли "
            "уведомлений, поэтому запуск отменён.\n"
            "Если старый бот завис, остановите его: pkill -f 'python bot.py'"
        )
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — панель мастера будет недоступна!")

    db.init_db()
    db.ensure_owner_master(config.ADMIN_IDS)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_handlers.router)
    dp.include_router(client_handlers.router)

    await setup_commands(bot)
    asyncio.create_task(scheduler.worker(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
