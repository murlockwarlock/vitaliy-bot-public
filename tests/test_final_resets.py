from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

import test_pr7_topic_lifecycle_and_settings as telegram
import funnel
import handlers
from database import (
    AiDisclaimerClaim, FunnelLanding, FunnelState, Habit, LandingNotificationClaim,
    Message, Transaction, User, UserSubscription, async_session_maker, init_db,
)
from time_utils import msk_now


@pytest_asyncio.fixture
async def chat():
    await init_db()
    client = telegram.TopicLifecycleAndSettingsE2ETests()
    client.tg = telegram.FakeTelegramSession()
    client.bot = Bot(token="123456:TEST_TOKEN", session=client.tg)
    client.storage = MemoryStorage()
    client.dp = Dispatcher(storage=client.storage)
    client._update_seq = 0
    routers = (funnel.router, handlers.router)
    parents = [router.parent_router for router in routers]
    try:
        for router in routers:
            router._parent_router = None
            client.dp.include_router(router)
        yield client
    finally:
        for router, parent in zip(routers, parents):
            router._parent_router = parent
        await client.storage.close()
        await client.bot.session.close()


async def seed_client(uid):
    now = msk_now()
    async with async_session_maker() as db:
        db.add(User(id=uid, first_name="Клиент", name="Имя", gender="male", balance=97,
                    main_reminder_count=4, next_main_reminder_at=now, subscription_end=now + timedelta(days=20),
                    welcome_shown_at=now, ai_disclaimer_shown=True,
                    ai_disclaimer_accepted_at=now, ai_disclaimer_version=1))
        await db.flush()
        opened = FunnelLanding(user_id=uid, token=f"opened-{uid}")
        paid = FunnelLanding(user_id=uid, token=f"paid-{uid}", status="paid", paid_at=now)
        db.add_all([opened, paid, UserSubscription(user_id=uid, end_date=now + timedelta(days=20),
                                                  payment_provider="Prodamus", auto_renewal=True),
                    FunnelState(user_id=uid, stage="active", is_active=True, next_followup_at=now),
                    Habit(user_id=uid, name="Чтение"), Message(user_id=uid, content="Сообщение", role="user"),
                    Transaction(user_id=uid, amount=97, status="success"),
                    AiDisclaimerClaim(user_id=uid, status="delivered")])
        await db.flush()
        db.add(LandingNotificationClaim(landing_id=opened.id, event_type="form_submitted"))
        await db.commit()


async def snapshot(uid):
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        sub = user.subscription
        fs = await db.get(FunnelState, uid)
        return {
            "funnel": bool(fs and fs.is_active),
            "dialog": await db.scalar(select(Message.id).where(Message.user_id == uid)) is not None,
            "sub": bool(user.subscription_end and sub.auto_renewal and sub.end_date > msk_now()),
            "balance": user.balance,
            "habits": await db.scalar(select(Habit.id).where(Habit.user_id == uid)) is not None,
            "remind": (user.main_reminder_count, user.next_main_reminder_at is not None),
            "landing": await db.scalar(select(FunnelLanding.id).where(FunnelLanding.token == f"opened-{uid}")) is not None,
            "name": (user.name, user.gender),
            "welcome": await handlers._read_welcome_shown_at(db, user) is not None,
            "disclaimer": (user.ai_disclaimer_shown, user.ai_disclaimer_accepted_at is not None,
                           user.ai_disclaimer_version, await db.get(AiDisclaimerClaim, uid) is not None),
            "paid_landing": await db.scalar(select(FunnelLanding.id).where(FunnelLanding.token == f"paid-{uid}")),
            "receipt": (await db.execute(select(Transaction.id, Transaction.amount, Transaction.status)
                                         .where(Transaction.user_id == uid))).one(),
        }


SUCCESS = {
    "funnel": "🌀 Воронка сброшена", "dialog": "💬 Диалог очищен", "sub": "📅 Подписка сброшена",
    "balance": "💰 Баланс обнулён", "habits": "🎗 Привычки удалены", "remind": "🔔 Напоминания сброшены",
    "landing": "🌐 Лендинг очищен", "name": "👤 Имя и пол сброшены", "full": "⚡️ Полный сброс выполнен!",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SUCCESS)
async def test_confirmed_client_reset(chat, kind):
    admin = 999999999
    target = 10**10 + uuid4().int % 10**10
    other = target + 10**10
    for uid in (target, other):
        await seed_client(uid)
        key = StorageKey(bot_id=chat.bot.id, chat_id=uid, user_id=uid)
        await chat.storage.set_state(key, "UserStates:chatting")
        await chat.storage.set_data(key, {"old_dialogue": 5})

    before, neighbor = await snapshot(target), await snapshot(other)
    root = f"admin_user_resets_{target}"
    await chat.bot.send_message(admin, "Профиль клиента")
    await chat._click(admin, root)
    ask = f"confirm_ureset_{kind}_{target}"
    confirm = f"ureset_confirmed_{kind}_{target}"

    def buttons():
        return [item[1] for item in telegram._extract_buttons(chat._latest_chat_message(admin).reply_markup)]

    assert ask in buttons()
    await chat._click(admin, ask)
    assert confirm in buttons() and root in buttons()
    await chat._click(admin, root)
    assert await snapshot(target) == before
    await chat._click(admin, ask)
    assert await snapshot(target) == before
    events = await chat._click(admin, confirm)
    assert any(e.get("text") == SUCCESS[kind] and e["kind"] == "callback_answer" for e in events)
    assert ask in buttons()
    assert await snapshot(other) == neighbor
    assert await chat._state_data(other) == {"old_dialogue": 5}

    reset = {"funnel": False, "dialog": False, "sub": False, "balance": 0, "habits": False,
             "remind": (0, False), "landing": False, "name": (None, None)}
    expected = before.copy()
    if kind == "full":
        expected.update(reset, sub=True, welcome=False, disclaimer=(False, False, 0, False))
    else:
        expected[kind] = reset[kind]
    assert await snapshot(target) == expected
    if kind == "sub":
        async with async_session_maker() as db:
            user = await db.get(User, target)
            assert user.subscription_end is None
            assert user.subscription.end_date <= msk_now()
            assert not user.subscription.auto_renewal
    if kind == "full":
        assert await chat._state_of(target) is None
        assert await chat._state_data(target) == {}
        welcome_text, _ = await handlers.get_welcome_content_and_media()
        events = await chat._send_text(target, "/start")
        assert any(e.get("text") == welcome_text for e in events)
        assert any(e.get("text") == "Как к тебе обращаться?" for e in events)
        await chat._send_text(target, "Ольга")
        assert await chat._state_of(target) == funnel.ProfileStates.waiting_gender.state
        await chat._click(target, "gender_female")
        assert (await snapshot(target))["name"] == ("Ольга", "female")
        assert await chat._state_of(target) is None
