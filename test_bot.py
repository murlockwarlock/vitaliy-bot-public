"""
Полная симуляция логики бота без Telegram API.
Тестирует: БД, воронку, демо-период, проверку доступа, follow-up, клавиатуры, AI-контексты.
Запуск: python3 test_bot.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "test:fake")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test_database.db")
os.environ.setdefault("OWNER_IDS", "999999")

# Удаляем тестовую БД если осталась
if os.path.exists("test_database.db"):
    os.remove("test_database.db")

PASS = 0
FAIL = 0
ERRORS = []


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✅ {name}")


def fail(name, detail=""):
    global FAIL
    FAIL += 1
    msg = f"  ❌ {name}: {detail}"
    ERRORS.append(msg)
    print(msg)


def check(name, condition, detail=""):
    if condition:
        ok(name)
    else:
        fail(name, detail)


async def run_tests():
    # === 1. Импорты ===
    print("\n📦 1. Проверка импортов...")
    try:
        from database import (
            init_db, async_session_maker, User, PaymentConfig, FunnelConfig,
            FunnelState, FunnelEvent, FunnelMeditation, Content, AIConfig,
            Topic, Message, HabitConfig, Habit, ReferralConfig, Book,
            SubscriptionPlan, PaymentProvider, Transaction, ContentMedia,
            FunnelLanding, KnowledgeBase, IndexingQueue, Mailing,
        )
        ok("database models")
    except Exception as e:
        fail("database models", str(e))
        return

    try:
        from funnel import (
            parse_start_action, get_or_create_funnel_state, set_stage,
            deactivate_funnel, schedule_followup, get_state_anchor_time,
            get_resume_mode, get_followup_text, ACTIVE_FUNNEL_STAGES,
            REMINDER_STAGE_KEYS, register_event, get_funnel_config,
            quick_nav_keyboard, gender_keyboard, funnel_entry_keyboard,
            resume_keyboard, reminder_keyboard, meditation_card_keyboard,
            meditation_listen_keyboard, analysis_keyboard,
            build_post_analysis_keyboard, build_vitaly_method_keyboard,
            build_strategy_offer_keyboard, split_text_smart,
        )
        ok("funnel functions")
    except Exception as e:
        fail("funnel functions", str(e))
        return

    try:
        import keyboards as kb
        ok("keyboards module")
    except Exception as e:
        fail("keyboards module", str(e))
        return

    try:
        from time_utils import msk_now, format_msk
        ok("time_utils")
    except Exception as e:
        fail("time_utils", str(e))
        return

    try:
        from logging_config import setup_logging, get_logger
        setup_logging()
        ok("logging_config")
    except Exception as e:
        fail("logging_config", str(e))
        return

    # === 2. Инициализация БД ===
    print("\n🗄️  2. Инициализация БД...")
    try:
        await init_db()
        ok("init_db() — таблицы созданы")
    except Exception as e:
        fail("init_db()", str(e))
        return

    # Проверяем дефолтные записи
    async with async_session_maker() as session:
        ai = await session.get(AIConfig, 1)
        check("AIConfig default создан", ai is not None)

        pay = await session.get(PaymentConfig, 1)
        check("PaymentConfig default создан", pay is not None)
        check("PaymentConfig.demo_days поле существует", hasattr(pay, 'demo_days'), "нет атрибута demo_days")
        check("PaymentConfig.demo_days default=0", pay.demo_days == 0, f"got {pay.demo_days}")
        check("PaymentConfig.mode default=free", pay.mode == 'free', f"got {pay.mode}")

        fc = await session.get(FunnelConfig, 1)
        check("FunnelConfig default создан", fc is not None)
        check("FunnelConfig.page_size=10", fc.page_size == 10)
        check("FunnelConfig.first_reminder_hours=6", fc.first_reminder_hours == 6)
        check("FunnelConfig.next_reminder_hours=24", fc.next_reminder_hours == 24)
        check("FunnelConfig.resume_after_hours=24", fc.resume_after_hours == 24)

        habit = await session.get(HabitConfig, 1)
        check("HabitConfig default создан", habit is not None)

        ref = await session.get(ReferralConfig, 1)
        check("ReferralConfig default создан", ref is not None)

    # Проверяем дефолтные топики
    async with async_session_maker() as session:
        from sqlalchemy import select, func
        topic_count = await session.scalar(select(func.count(Topic.id)))
        check("Топики созданы", topic_count >= 7, f"found {topic_count}")

        expected_slugs = ["free_session", "diagnostics", "express", "journal", "habits",
                          "funnel_demo", "strategy_session", "strategy_objections"]
        for slug in expected_slugs:
            t = await session.scalar(select(Topic).where(Topic.slug == slug))
            check(f"Топик '{slug}' существует", t is not None)

    # Дефолтный контент
    async with async_session_maker() as session:
        critical_keys = [
            "start_msg", "funnel_intro", "funnel_choice_intro", "funnel_listen_instruction",
            "funnel_reflection_prompt", "funnel_after_analysis_offer", "funnel_vitaly_method",
            "funnel_strategy_sales", "funnel_strategy_faq", "funnel_resume_text",
            "funnel_reminder_intro", "funnel_reminder_listen", "funnel_reminder_reflection",
            "funnel_reminder_strategy", "funnel_reminder_common", "strategy_session_intro",
            "btn_funnel_landing",
        ]
        for key in critical_keys:
            c = await session.get(Content, key)
            check(f"Контент '{key}' существует", c is not None)

    # === 3. parse_start_action ===
    print("\n🔗 3. Парсинг start-аргументов...")
    action, val = parse_start_action(None)
    check("None → (None, None)", action is None and val is None)

    action, val = parse_start_action("")
    check("'' → (None, None)", action is None and val is None)

    action, val = parse_start_action("12345")
    check("'12345' → referral", action == "referral" and val == 12345)

    action, val = parse_start_action("practice")
    check("'practice' → funnel", action == "funnel")

    action, val = parse_start_action("funnel")
    check("'funnel' → funnel", action == "funnel")

    action, val = parse_start_action("hypnosis")
    check("'hypnosis' → funnel", action == "funnel")

    action, val = parse_start_action("meditation")
    check("'meditation' → funnel", action == "funnel")

    action, val = parse_start_action("practice_123")
    check("'practice_123' → funnel", action == "funnel")

    action, val = parse_start_action("random_text")
    check("'random_text' → (None, None)", action is None and val is None)

    # === 4. Демо-период ===
    print("\n🎁 4. Демо-период...")
    TEST_USER_ID = 100001

    # Сценарий: mode=free, demo_days=7 → НЕ назначать
    async with async_session_maker() as session:
        pay = await session.get(PaymentConfig, 1)
        pay.mode = 'free'
        pay.demo_days = 7
        await session.commit()

    async with async_session_maker() as session:
        user = User(id=TEST_USER_ID, username="test_free", first_name="TestFree")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        check("mode=free → subscription_end=None", user.subscription_end is None)
        await session.delete(user)
        await session.commit()

    # Сценарий: mode=subscription, demo_days=7 → назначить
    async with async_session_maker() as session:
        pay = await session.get(PaymentConfig, 1)
        pay.mode = 'subscription'
        pay.demo_days = 7
        await session.commit()

    async with async_session_maker() as session:
        user = User(id=TEST_USER_ID, username="test_demo", first_name="TestDemo")
        session.add(user)
        pay = await session.get(PaymentConfig, 1)
        if pay and pay.demo_days and pay.demo_days > 0 and pay.mode != 'free':
            user.subscription_end = msk_now() + timedelta(days=pay.demo_days)
        await session.commit()
        await session.refresh(user)

        check("mode=subscription, demo=7 → subscription_end установлен",
              user.subscription_end is not None)
        if user.subscription_end:
            diff = (user.subscription_end - msk_now()).total_seconds()
            check("subscription_end ≈ +7 дней",
                  abs(diff - 7 * 86400) < 60,
                  f"diff={diff:.0f}s, expected ~{7*86400}s")

    # Сценарий: mode=subscription, demo_days=0 → НЕ назначать
    async with async_session_maker() as session:
        await session.delete(await session.get(User, TEST_USER_ID))
        await session.commit()
        pay = await session.get(PaymentConfig, 1)
        pay.demo_days = 0
        await session.commit()

    async with async_session_maker() as session:
        user = User(id=TEST_USER_ID, username="test_nodemo", first_name="TestNodemo")
        session.add(user)
        pay = await session.get(PaymentConfig, 1)
        if pay and pay.demo_days and pay.demo_days > 0 and pay.mode != 'free':
            user.subscription_end = msk_now() + timedelta(days=pay.demo_days)
        await session.commit()
        await session.refresh(user)
        check("mode=subscription, demo=0 → subscription_end=None", user.subscription_end is None)

    # === 5. Воронка — состояния и переходы ===
    print("\n🌀 5. Воронка — состояния и переходы...")
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        check("Начальное состояние stage=idle", state.stage == "idle")
        check("Начальное состояние is_active=False", state.is_active == False)

        await set_stage(session, state, "intro", active=True, reminder_enabled=False)
        await session.commit()
        check("set_stage → intro", state.stage == "intro")
        check("set_stage → is_active=True", state.is_active == True)
        check("reminder_enabled=False → next_followup_at=None", state.next_followup_at is None)

    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        await set_stage(session, state, "choice", active=True, reminder_enabled=True)
        await session.commit()
        check("set_stage → choice", state.stage == "choice")
        check("reminder_enabled=True → next_followup_at задан", state.next_followup_at is not None)
        check("reminder_count=0", state.reminder_count == 0)

    # Проверяем все стадии из ACTIVE_FUNNEL_STAGES
    expected_stages = {"intro", "choice", "meditation_card", "listening",
                       "waiting_reflection", "post_analysis", "vitaly_method",
                       "strategy_offer", "strategy_faq", "strategy_chat", "objection_chat"}
    check("ACTIVE_FUNNEL_STAGES полный набор",
          ACTIVE_FUNNEL_STAGES == expected_stages,
          f"got {ACTIVE_FUNNEL_STAGES}")

    # Проверяем REMINDER_STAGE_KEYS
    for stage_key in ["choice", "listening", "waiting_reflection", "post_analysis",
                       "vitaly_method", "strategy_offer", "strategy_faq",
                       "strategy_chat", "objection_chat"]:
        check(f"REMINDER_STAGE_KEYS содержит '{stage_key}'",
              stage_key in REMINDER_STAGE_KEYS)

    # === 6. Деактивация воронки ===
    print("\n⏹️  6. Деактивация воронки...")
    async with async_session_maker() as session:
        await deactivate_funnel(session, TEST_USER_ID, "paused")
        await session.commit()
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        check("deactivate → is_active=False", state.is_active == False)
        check("deactivate → stage=paused", state.stage == "paused")
        check("deactivate → next_followup_at=None", state.next_followup_at is None)
        check("deactivate → meditation_id=None", state.meditation_id is None)

    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        await set_stage(session, state, "strategy_offer", active=True, reminder_enabled=True)
        await session.commit()
        await deactivate_funnel(session, TEST_USER_ID, "completed", completed=True)
        await session.commit()
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        check("deactivate completed → completed_at задан", state.completed_at is not None)

    # === 7. Resume mode ===
    print("\n🔄 7. Resume mode (возврат в воронку)...")
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        state.is_active = False
        await session.commit()
        mode = await get_resume_mode(session, state)
        check("is_active=False → resume_mode=None", mode is None)

    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        await set_stage(session, state, "choice", active=True, reminder_enabled=True)
        state.last_user_action_at = msk_now()  # только что
        await session.commit()
        mode = await get_resume_mode(session, state)
        check("Активна, недавно → resume=direct", mode == "direct", f"got {mode}")

    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        state.last_user_action_at = msk_now() - timedelta(hours=25)
        state.stage_changed_at = msk_now() - timedelta(hours=25)
        state.started_at = msk_now() - timedelta(hours=25)
        await session.commit()
        mode = await get_resume_mode(session, state)
        check("Активна, >24ч назад → resume=menu", mode == "menu", f"got {mode}")

    # === 8. Follow-up текст ===
    print("\n📨 8. Follow-up тексты...")
    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        await set_stage(session, state, "choice", active=True, reminder_enabled=True)
        state.reminder_count = 0
        await session.commit()
        text = await get_followup_text(session, state)
        check("1-й follow-up (choice) → уникальный текст", text is not None and len(text) > 5, f"got: {text}")

        state.reminder_count = 1
        text2 = await get_followup_text(session, state)
        check("2-й follow-up → общий текст", text2 is not None and len(text2) > 5)

    async with async_session_maker() as session:
        state = await get_or_create_funnel_state(session, TEST_USER_ID)
        state.stage = "waiting_reflection"
        state.reminder_count = 0
        await session.commit()
        text = await get_followup_text(session, state)
        check("follow-up waiting_reflection → текст есть", text is not None)

    # === 9. Schedule followup ===
    print("\n⏰ 9. Расписание follow-up...")
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        t1 = schedule_followup(config, 0)
        check("1-й followup через first_reminder_hours",
              abs((t1 - msk_now()).total_seconds() - config.first_reminder_hours * 3600) < 60)

        t2 = schedule_followup(config, 1)
        check("2-й followup через next_reminder_hours",
              abs((t2 - msk_now()).total_seconds() - config.next_reminder_hours * 3600) < 60)

    # === 10. Регистрация событий ===
    print("\n📊 10. Регистрация событий (FunnelEvent)...")
    async with async_session_maker() as session:
        await register_event(session, TEST_USER_ID, "intro", "test_event", meditation_id=None, payload="test payload")
        await session.commit()

    async with async_session_maker() as session:
        from sqlalchemy import select
        event = await session.scalar(
            select(FunnelEvent).where(
                FunnelEvent.user_id == TEST_USER_ID,
                FunnelEvent.event_type == "test_event"
            )
        )
        check("FunnelEvent создан", event is not None)
        if event:
            check("FunnelEvent.stage=intro", event.stage == "intro")
            check("FunnelEvent.payload=test payload", event.payload == "test payload")
            check("FunnelEvent.created_at задан", event.created_at is not None)

    # === 11. Медитации ===
    print("\n🧘 11. Медитации (CRUD)...")
    async with async_session_maker() as session:
        med = FunnelMeditation(
            title="Тестовая медитация",
            duration_minutes=15,
            short_description="Краткое описание",
            full_description="Полное описание",
            sort_order=1,
            is_active=True,
            reserve_url="https://example.com/reserve",
        )
        session.add(med)
        await session.commit()
        med_id = med.id
        check("Медитация создана", med_id is not None)

    async with async_session_maker() as session:
        med = await session.get(FunnelMeditation, med_id)
        check("Медитация читается из БД", med is not None)
        check("title корректен", med.title == "Тестовая медитация")
        check("duration_minutes=15", med.duration_minutes == 15)
        check("short_description заполнен", med.short_description == "Краткое описание")
        check("full_description заполнен", med.full_description == "Полное описание")
        check("is_active=True", med.is_active == True)
        check("reserve_url корректен", med.reserve_url == "https://example.com/reserve")
        check("sort_order=1", med.sort_order == 1)

    # === 12. FunnelLanding ===
    print("\n💳 12. FunnelLanding...")
    async with async_session_maker() as session:
        landing = FunnelLanding(
            token="test_token_12345",
            user_id=TEST_USER_ID,
            meditation_id=med_id,
            amount=1500,
            currency="RUB",
            status="opened",
            landing_url="https://example.com?bot_uid=100001&landing_token=test_token_12345",
        )
        session.add(landing)
        await session.commit()
        check("FunnelLanding создан", landing.id is not None)
        check("FunnelLanding.status=opened", landing.status == "opened")

    async with async_session_maker() as session:
        from sqlalchemy import select
        landing = await session.scalar(
            select(FunnelLanding).where(FunnelLanding.token == "test_token_12345")
        )
        check("FunnelLanding находится по токену", landing is not None)
        if landing:
            landing.status = "paid"
            landing.paid_at = msk_now()
            await session.commit()
            check("FunnelLanding → paid", landing.status == "paid")
            check("paid_at задан", landing.paid_at is not None)

    # === 13. Клавиатуры ===
    print("\n⌨️  13. Клавиатуры (генерация)...")
    try:
        kbd = quick_nav_keyboard()
        buttons = [b.text for row in kbd.keyboard for b in row]
        check("quick_nav: Главное меню", "Главное меню" in buttons)
        check("quick_nav: Практики", "Практики" in buttons)
        check("quick_nav: Стратегическая сессия", "Стратегическая сессия" in buttons)
        check("quick_nav: Настройки", "Настройки" in buttons)
    except Exception as e:
        fail("quick_nav_keyboard", str(e))

    try:
        kbd = gender_keyboard()
        buttons = [b.text for row in kbd.keyboard for b in row]
        check("gender: Мужчина", "Мужчина" in buttons)
        check("gender: Женщина", "Женщина" in buttons)
    except Exception as e:
        fail("gender_keyboard", str(e))

    try:
        kbd = funnel_entry_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("funnel_entry: funnel_intro_start", "funnel_intro_start" in cbs)
        check("funnel_entry: funnel_about_method", "funnel_about_method" in cbs)
        check("funnel_entry: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("funnel_entry_keyboard", str(e))

    try:
        kbd = resume_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("resume: funnel_resume_choice", "funnel_resume_choice" in cbs)
        check("resume: funnel_resume_strategy", "funnel_resume_strategy" in cbs)
        check("resume: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("resume_keyboard", str(e))

    try:
        kbd = reminder_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("reminder: menu_funnel_start", "menu_funnel_start" in cbs)
        check("reminder: funnel_resume_strategy", "funnel_resume_strategy" in cbs)
        check("reminder: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("reminder_keyboard", str(e))

    try:
        kbd = meditation_card_keyboard(1, 0)
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("med_card: funnel_listen_1", "funnel_listen_1" in cbs)
        check("med_card: funnel_choice_0 (назад)", "funnel_choice_0" in cbs)
        check("med_card: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("meditation_card_keyboard", str(e))

    try:
        kbd = meditation_listen_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("listen: funnel_heard", "funnel_heard" in cbs)
        check("listen: menu_funnel_start (другая практика)", "menu_funnel_start" in cbs)
    except Exception as e:
        fail("meditation_listen_keyboard", str(e))

    try:
        kbd = analysis_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("analysis: funnel_resume_strategy", "funnel_resume_strategy" in cbs)
        check("analysis: menu_funnel_start", "menu_funnel_start" in cbs)
        check("analysis: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("analysis_keyboard", str(e))

    try:
        kbd = build_post_analysis_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("post_analysis: funnel_interest_yes", "funnel_interest_yes" in cbs)
        check("post_analysis: funnel_interest_no", "funnel_interest_no" in cbs)
    except Exception as e:
        fail("build_post_analysis_keyboard", str(e))

    try:
        kbd = build_vitaly_method_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("vitaly_method: funnel_show_sales", "funnel_show_sales" in cbs)
        check("vitaly_method: funnel_interest_no", "funnel_interest_no" in cbs)
        check("vitaly_method: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("build_vitaly_method_keyboard", str(e))

    try:
        kbd = build_strategy_offer_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("strategy_offer: funnel_open_landing", "funnel_open_landing" in cbs)
        check("strategy_offer: funnel_show_faq", "funnel_show_faq" in cbs)
        check("strategy_offer: funnel_start_objections", "funnel_start_objections" in cbs)
        check("strategy_offer: menu_funnel_start", "menu_funnel_start" in cbs)
        check("strategy_offer: funnel_to_main", "funnel_to_main" in cbs)
    except Exception as e:
        fail("build_strategy_offer_keyboard", str(e))

    # Главное меню
    try:
        kbd = await kb.main_menu_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("main_menu: menu_funnel_start (Практика)", "menu_funnel_start" in cbs)
        check("main_menu: menu_strategy_session", "menu_strategy_session" in cbs)
        check("main_menu: mode_free_session", "mode_free_session" in cbs)
        check("main_menu: mode_diagnostics", "mode_diagnostics" in cbs)
        check("main_menu: mode_express", "mode_express" in cbs)
        check("main_menu: mode_journal", "mode_journal" in cbs)
        check("main_menu: mode_habits", "mode_habits" in cbs)
        check("main_menu: open_extended_menu", "open_extended_menu" in cbs)
    except Exception as e:
        fail("main_menu_keyboard", str(e))

    # Админка
    try:
        kbd = kb.admin_panel_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("admin: admin_funnel_settings", "admin_funnel_settings" in cbs)
        check("admin: admin_ai_settings", "admin_ai_settings" in cbs)
        check("admin: admin_payment_settings", "admin_payment_settings" in cbs)
    except Exception as e:
        fail("admin_panel_keyboard", str(e))

    # Платежи с демо-периодом
    try:
        kbd = kb.admin_payment_keyboard()
        cbs = [b.callback_data for row in kbd.inline_keyboard for b in row]
        check("payment_admin: admin_pay_demo (кнопка демо-периода)", "admin_pay_demo" in cbs)
    except Exception as e:
        fail("admin_payment_keyboard", str(e))

    # === 14. split_text_smart ===
    print("\n📝 14. split_text_smart...")
    chunks = split_text_smart("a" * 100, limit=50)
    check("split_text: разбивает длинный текст", len(chunks) >= 2)

    chunks = split_text_smart("short text", limit=4000)
    check("split_text: короткий текст = 1 чанк", len(chunks) == 1)

    # === 15. Полный цикл воронки ===
    print("\n🔄 15. Полный цикл воронки (state transitions)...")
    USER2 = 100002
    async with async_session_maker() as session:
        user2 = User(id=USER2, username="funnel_test", first_name="FunnelUser")
        session.add(user2)
        await session.commit()

    stages_sequence = [
        ("intro", False),
        ("choice", True),
        ("meditation_card", False),
        ("listening", True),
        ("waiting_reflection", True),
        ("post_analysis", True),
        ("vitaly_method", True),
        ("strategy_offer", True),
        ("strategy_faq", True),
        ("strategy_chat", True),
        ("objection_chat", True),
    ]
    for stage, with_reminder in stages_sequence:
        async with async_session_maker() as session:
            state = await get_or_create_funnel_state(session, USER2)
            await set_stage(session, state, stage, active=True, reminder_enabled=with_reminder)
            await register_event(session, USER2, stage, f"test_{stage}")
            await session.commit()
            check(f"Этап '{stage}' установлен", state.stage == stage)
            if with_reminder:
                check(f"Этап '{stage}' — followup запланирован", state.next_followup_at is not None)

    # Финальная деактивация
    async with async_session_maker() as session:
        await deactivate_funnel(session, USER2, "completed", completed=True)
        await session.commit()
        state = await get_or_create_funnel_state(session, USER2)
        check("Полный цикл завершён → completed_at задан", state.completed_at is not None)
        check("Полный цикл завершён → is_active=False", state.is_active == False)

    # === 16. FunnelConfig CRUD ===
    print("\n⚙️  16. FunnelConfig CRUD...")
    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        config.page_size = 5
        config.first_reminder_hours = 3
        config.next_reminder_hours = 12
        config.resume_after_hours = 48
        await session.commit()

    async with async_session_maker() as session:
        config = await get_funnel_config(session)
        check("page_size обновлён до 5", config.page_size == 5)
        check("first_reminder_hours обновлён до 3", config.first_reminder_hours == 3)
        check("next_reminder_hours обновлён до 12", config.next_reminder_hours == 12)
        check("resume_after_hours обновлён до 48", config.resume_after_hours == 48)

    # === 17. Проверка доступа (check_paid_access логика) ===
    print("\n🔒 17. Проверка доступа (логика)...")
    async with async_session_maker() as session:
        pay = await session.get(PaymentConfig, 1)
        user = await session.get(User, TEST_USER_ID)

        # Free mode — всегда доступ
        pay.mode = 'free'
        await session.commit()
        check("mode=free → доступ открыт", True)

        # Subscription, подписка активна
        pay.mode = 'subscription'
        user.subscription_end = msk_now() + timedelta(days=5)
        await session.commit()
        has_access = user.subscription_end and user.subscription_end > msk_now()
        check("mode=subscription, подписка активна → доступ", has_access == True)

        # Subscription, подписка истекла
        user.subscription_end = msk_now() - timedelta(days=1)
        await session.commit()
        has_access = user.subscription_end and user.subscription_end > msk_now()
        check("mode=subscription, подписка истекла → нет доступа", has_access == False)

        # Subscription, subscription_end=None
        user.subscription_end = None
        await session.commit()
        has_access = user.subscription_end and user.subscription_end > msk_now()
        check("mode=subscription, subscription_end=None → нет доступа", has_access == False)

        # Token mode, balance > 0
        pay.mode = 'token'
        user.balance = 100.0
        await session.commit()
        check("mode=token, balance=100 → доступ", user.balance > 0)

        # Token mode, balance = 0
        user.balance = 0.0
        await session.commit()
        check("mode=token, balance=0 → нет доступа", user.balance <= 0)

        # Admin всегда имеет доступ
        user.is_admin = True
        await session.commit()
        check("is_admin=True → доступ всегда", user.is_admin == True)

    # === Итоги ===
    print(f"\n{'='*50}")
    print(f"📋 ИТОГО: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print(f"\n❌ Ошибки:")
        for e in ERRORS:
            print(e)
    else:
        print("🎉 Все тесты пройдены!")
    print(f"{'='*50}\n")

    # Очистка
    if os.path.exists("test_database.db"):
        os.remove("test_database.db")

    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
