"""Entry point: starts the bot and the background notification loop."""
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
    """A second bot instance = a second scheduler = duplicate notifications. Block it.

    The lock is released automatically when the process exits (even on kill -9).
    """
    global _lock_handle
    # "a+", not "w": mode "w" would truncate the file before the lock is even
    # acquired, so a failed second start would wipe the running bot's PID.
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
    await bot.set_my_commands([BotCommand(command="start", description="Menu")])
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Menu"),
                    BotCommand(command="admin", description="Owner panel"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            pass  # admin hasn't opened a chat with the bot yet


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Copy .env.example to .env and paste the token from @BotFather."
        )
    if not acquire_single_instance():
        raise SystemExit(
            "The bot is already running in another process — a second instance "
            "would send duplicate notifications, so startup was aborted.\n"
            "If the old bot is stuck, stop it: pkill -f 'python bot.py'"
        )
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty — the owner panel will be unavailable!")

    db.init_db()
    db.ensure_owner_master(config.ADMIN_IDS)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_handlers.router)
    dp.include_router(client_handlers.router)

    await setup_commands(bot)
    asyncio.create_task(scheduler.worker(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
