import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from config import BOT_TOKEN, OWNER_IDS
from funnel import router as funnel_router, process_funnel_followups
from landing_webhook import run_landing_webhook_server
from logging_config import setup_logging, get_logger
from handlers import router
from journal_handlers import router as journal_router
from journal_admin_handlers import router as journal_admin_router
from test_system_handlers import router as test_system_router
from topic_admin_handlers import router as topic_admin_router
from test_set_admin_handlers import router as test_set_admin_router
from database import init_db, async_session_maker, User
from background_worker import process_queue, process_mailings, habits_worker, payment_monitor_worker, main_bot_reminder_worker, journal_reminder_worker
from subscription_scheduler import subscription_scheduler_worker
from telegram_client import create_telegram_bot

logger = get_logger("bot")


async def set_commands(bot: Bot):
    # 1. Команды для обычных пользователей (по умолчанию)
    user_commands = [
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="menu", description="📱 Главное меню"),
        BotCommand(command="settings", description="⚙️ Настройки профиля"),
        BotCommand(command="subscription", description="💳 Подписка"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # 2. Команды для админов
    admin_commands = [
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="menu", description="📱 Главное меню"),
        BotCommand(command="settings", description="⚙️ Настройки профиля"),
        BotCommand(command="subscription", description="💳 Подписка"),
        BotCommand(command="admin", description="⚙️ Админ-панель"),
        BotCommand(command="help", description="📖 Инструкция по настройке"),
    ]

    # Собираем список всех админов (Владельцы + Админы из БД)
    admin_ids = set(OWNER_IDS)

    async with async_session_maker() as session:
        # Ищем тех, кого назначили админом через панель
        result = await session.execute(select(User.id).where(User.is_admin == True))
        db_admins = result.scalars().all()
        for admin_id in db_admins:
            admin_ids.add(admin_id)

    # Проставляем персональное меню каждому админу
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning(f"Не удалось обновить меню для админа {admin_id}: {e}")


async def main():
    setup_logging()
    logger.info("Запуск бота")

    await init_db()

    bot = create_telegram_bot(BOT_TOKEN)
    from error_reporting import set_active_bot
    set_active_bot(bot)
    from followup_admin import router as followup_admin_router
    from ai_logs_admin import router as ai_logs_admin_router
    from automation_admin import router as automation_admin_router
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(funnel_router)
    dp.include_router(test_system_router)
    dp.include_router(topic_admin_router)
    dp.include_router(test_set_admin_router)
    dp.include_router(followup_admin_router)
    dp.include_router(ai_logs_admin_router)
    dp.include_router(automation_admin_router)
    dp.include_router(journal_admin_router)
    dp.include_router(journal_router)
    dp.include_router(router)

    await set_commands(bot)

    asyncio.create_task(process_queue(bot))
    asyncio.create_task(process_mailings(bot))
    asyncio.create_task(habits_worker(bot))
    asyncio.create_task(payment_monitor_worker(bot))
    asyncio.create_task(process_funnel_followups(bot))
    asyncio.create_task(main_bot_reminder_worker(bot))
    asyncio.create_task(subscription_scheduler_worker(bot))
    asyncio.create_task(run_landing_webhook_server(bot))
    from followups import followup_worker
    asyncio.create_task(followup_worker(bot))
    asyncio.create_task(journal_reminder_worker(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
