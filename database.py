import asyncio
import json
import uuid
from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, BigInteger, Table, text, Float, Date, select, MetaData, update, delete, UniqueConstraint, event, Index, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, selectinload, backref, synonym, deferred
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.exc import IntegrityError
from config import DATABASE_URL
from crisis_safety import DEFAULT_CRISIS_MARKERS
from time_utils import msk_now
from universal_tests import is_esoteric_question, is_test_set_ready

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()
    except Exception:
        pass

Base = declarative_base()

topic_knowledgebase_association = Table(
    'topic_kb_association',
    Base.metadata,
    Column('topic_id', Integer, ForeignKey('topics.id'), primary_key=True),
    Column('knowledge_base_id', Integer, ForeignKey('knowledge_base.id'), primary_key=True)
)

promocode_plan_association = Table(
    'promocode_plan_association',
    Base.metadata,
    Column('promo_code_id', Integer, ForeignKey('promo_codes.id'), primary_key=True),
    Column('plan_id', Integer, ForeignKey('subscription_plans.id'), primary_key=True)
)

user_promo_association = Table(
    'user_promo_association',
    Base.metadata,
    Column('user_id', BigInteger, ForeignKey('users.id'), primary_key=True),
    Column('promo_code_id', Integer, ForeignKey('promo_codes.id'), primary_key=True)
)


class User(Base):
    __tablename__ = 'users'
    __table_args__ = {"implicit_returning": False}
    id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String)
    name = Column(String, nullable=True)
    gender = Column(String, nullable=True)
    age = Column(String, nullable=True)
    response_length = Column(String, default='normal', nullable=False)
    is_admin = Column(Boolean, default=False)
    admin_role = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=msk_now)
    messages = relationship("Message", back_populates="user")
    current_dialogue_id = Column(Integer, default=1, nullable=False)
    current_topic_slug = Column(String, ForeignKey('topics.slug'), nullable=True)
    current_topic = relationship("Topic")
    habit_timezone = Column(String(64), nullable=True)
    referrer_id = Column(BigInteger, nullable=True)
    referral_balance = Column(Integer, default=0)
    balance = Column(Float, default=0.0)
    subscription_end = Column(DateTime, nullable=True)
    low_balance_notified = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)
    last_activity_at = Column(DateTime, nullable=True)
    main_reminder_count = Column(Integer, default=0)
    next_main_reminder_at = Column(DateTime, nullable=True)
    ai_disclaimer_shown = Column(Boolean, default=False, nullable=False)
    ai_disclaimer_accepted_at = Column(DateTime, nullable=True)
    ai_disclaimer_version = Column(Integer, default=0, nullable=False)
    journal_points = Column(Integer, default=0, nullable=False)
    welcome_shown_at = deferred(Column(DateTime, nullable=True, server_default=text("NULL")))
    crisis_detected_at = Column(DateTime, nullable=True)
    subscription = relationship("UserSubscription", back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin")
    promo_codes = relationship("PromoCode", secondary=user_promo_association, back_populates="users", lazy="selectin")


class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'))
    dialogue_id = Column(Integer, default=1, nullable=False)
    role = Column(String)
    content = Column(Text)
    timestamp = Column(DateTime, default=msk_now)
    user = relationship("User", back_populates="messages")
    topic_slug = Column(String, nullable=True)
    message_kind = Column(String, default='standard', nullable=False, index=True)
    test_attempt_id = Column(Integer, ForeignKey('test_attempts.id', ondelete='SET NULL'), nullable=True, index=True)


class AIConfig(Base):
    __tablename__ = 'ai_config'
    id = Column(Integer, primary_key=True, default=1)
    provider = Column(String, default='OpenAI')
    openai_api_key = Column(String, nullable=True)
    openai_model = Column(String, default='gpt-4o')
    deepseek_api_key = Column(String, nullable=True)
    deepseek_model = Column(String, default='deepseek-chat')
    kie_api_key = Column(String, nullable=True)
    kie_model = Column(String, default='gemini-3-flash')
    kie_base_url = Column(String, default='https://api.kie.ai')
    kie_upload_base_url = Column(String, default='https://kieai.redpandaai.co')
    kie_transcription_model = Column(String, default='elevenlabs/speech-to-text')
    kie_credit_alert_threshold = Column(Float, default=0, nullable=False)
    kie_credit_alert_sent = Column(Boolean, default=False, nullable=False)
    openrouter_api_key = Column(String, nullable=True)
    openrouter_model = Column(String, nullable=True)
    transcription_provider = Column(String, default='OpenAI', nullable=False)
    vision_provider = Column(String, default='KIE', nullable=False)
    vision_model = Column(String, default='gemini-3-flash', nullable=False)
    image_generation_provider = Column(String, default='KIE', nullable=False)
    image_generation_model = Column(String, default='seedream/4.5-text-to-image', nullable=False)
    image_edit_provider = Column(String, default='KIE', nullable=False)
    image_edit_model = Column(String, default='seedream/4.5-edit', nullable=False)
    max_voice_duration_sec = Column(Integer, default=180, nullable=False)
    temperature = Column(Float, default=0.7, nullable=False)
    fallback_provider = Column(String, nullable=True)
    fallback_model = Column(String, nullable=True)
    allow_fallback = Column(Boolean, default=False, nullable=False)
    context_limit_first = Column(Integer, default=2)
    context_limit_recent = Column(Integer, default=10)


class KnowledgeBase(Base):
    __tablename__ = 'knowledge_base'
    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String)
    indexed_content = Column(Text)
    uploaded_at = Column(DateTime, default=msk_now)
    topics = relationship("Topic", secondary=topic_knowledgebase_association, back_populates="knowledge_base_files")


class Topic(Base):
    __tablename__ = 'topics'
    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    system_prompt = Column(Text, nullable=True)
    is_paid = Column(Boolean, default=True)
    knowledge_base_files = relationship("KnowledgeBase", secondary=topic_knowledgebase_association,
                                        back_populates="topics")


class Content(Base):
    __tablename__ = 'content'
    key = Column(String, primary_key=True)
    text_content = Column(Text, nullable=True)
    button_text = Column(String, nullable=True)
    link_url = Column(String, nullable=True)
    content_order = Column(String, default='media_top')
    media = relationship("ContentMedia", back_populates="content", cascade="all, delete-orphan")


class ContentMedia(Base):
    __tablename__ = 'content_media'
    id = Column(Integer, primary_key=True, autoincrement=True)
    content_key = Column(String, ForeignKey('content.key'), nullable=False)
    file_type = Column(String, nullable=False)
    file_id = Column(String, nullable=False)
    content = relationship("Content", back_populates="media")


class MenuSection(Base):
    """Admin-managed menu/section container.

    Keys are stable internal identifiers for callbacks and migrations. They are
    never shown to administrators; the UI uses ``title`` instead.
    """
    __tablename__ = 'menu_sections'
    key = Column(String(64), primary_key=True)
    title = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    order_num = Column(Integer, default=0, nullable=False, index=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now, nullable=False)


class MenuButton(Base):
    """One editable button while preserving its legacy action callback."""
    __tablename__ = 'menu_buttons'
    __table_args__ = (
        Index(
            'uq_menu_button_book_source',
            'section_key',
            'source_type',
            'source_id',
            unique=True,
            sqlite_where=text("source_type = 'book' AND source_id IS NOT NULL AND enabled = 1"),
            postgresql_where=text("source_type = 'book' AND source_id IS NOT NULL AND enabled = TRUE"),
        ),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    section_key = Column(String(64), ForeignKey('menu_sections.key', ondelete='CASCADE'), nullable=False, index=True)
    label = Column(String(255), nullable=False)
    action_type = Column(String(16), default='callback', nullable=False)
    action_value = Column(Text, nullable=True)
    content_key = Column(String(128), nullable=True)
    source_type = Column(String(32), default='static', nullable=False)
    source_id = Column(Integer, nullable=True, index=True)
    legacy_callback = Column(String(255), nullable=True, index=True)
    label_override = Column(Boolean, default=False, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    order_num = Column(Integer, default=0, nullable=False, index=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now, nullable=False)


class MenuAnchor(Base):
    __tablename__ = 'menu_anchors'
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(Integer, nullable=False)
    message_kind = Column(String(16), nullable=False)
    media_file_id = Column(String, nullable=True)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now, nullable=False)


class AdminNavigationContext(Base):
    __tablename__ = 'admin_navigation_contexts'

    token = Column(String(32), primary_key=True)
    admin_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    message_id = Column(Integer, nullable=False)
    return_callback = Column(String(255), nullable=False)
    parent_callback = Column(String(255), nullable=True, default="admin_panel")
    search_query = Column(Text, nullable=True)
    reset_callback = Column(String(255), nullable=False)
    reset_query = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False, index=True)


class ReferralConfig(Base):
    __tablename__ = 'referral_config'
    id = Column(Integer, primary_key=True, default=1)
    points_per_invite = Column(Integer, default=10)


class IndexingQueue(Base):
    __tablename__ = 'indexing_queue'
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    uploader_id = Column(BigInteger, ForeignKey('users.id'))
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=msk_now)


class Mailing(Base):
    __tablename__ = 'mailings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=True)
    media_file_id = Column(String, nullable=True)
    media_file_type = Column(String, nullable=True)
    button_text = Column(String, nullable=True)
    button_action = Column(String, nullable=True)
    created_at = Column(DateTime, default=msk_now)
    target_audience = Column(String, default='all')  # all, me, active, inactive, no_sub, has_sub, no_funnel, paid_funnel
    status = Column(String, default='pending')
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)


class HabitConfig(Base):
    __tablename__ = 'habit_config'
    id = Column(Integer, primary_key=True, default=1)
    mode = Column(String, default='hybrid')
    is_paid = Column(Boolean, default=True)

    # Настройки Геймификации
    streak_days = Column(Integer, default=7)  # Сколько дней длится цикл (7 или 10)
    reward_sequence = Column(String, default="М,О,Л,О,Д,Е,Ц")  # Символы через запятую
    time_window_minutes = Column(Integer, default=60)  # Время на выполнение (минуты)
    final_message = Column(Text, default="Поздравляем! Ты прошёл(ла) путь героя и закрепил(а) привычку! 🏆")


class Habit(Base):
    __tablename__ = 'habits'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    name = Column(String, nullable=False)
    time_target = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=msk_now)
    next_reminder_at = Column(DateTime, nullable=True)
    last_reminder_at = Column(DateTime, nullable=True)

    # Состояние прогресса
    current_streak = Column(Integer, default=0)  # Текущий день серии
    last_completed_date = Column(Date, nullable=True)  # Дата последнего выполнения


class PaymentConfig(Base):
    __tablename__ = 'payment_config'
    id = Column(Integer, primary_key=True, default=1)
    mode = Column(String, default='free')
    currency_ratio = Column(Float, default=1.0)
    cost_per_message = Column(Float, default=1.0)
    low_balance_threshold = Column(Float, default=10.0)
    subscription_price = Column(Integer, default=29900)
    subscription_currency = Column(String, default='RUB')
    demo_days = Column(Integer, default=0)


class PaymentProvider(Base):
    __tablename__ = 'payment_providers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    provider_token = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)


class SubscriptionPlan(Base):
    __tablename__ = 'subscription_plans'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)  # Цена в рублях
    duration_value = Column(Integer, nullable=False, default=30)
    duration_unit = Column(String, nullable=False, default='days')  # 'days' или 'months'
    is_active = Column(Boolean, default=True)
    admin_only = Column(Boolean, default=False, nullable=False)
    is_trial = Column(Boolean, default=False, nullable=False)
    upgrades_to_plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True)
    upgrades_to_plan = relationship(
        "SubscriptionPlan",
        foreign_keys=[upgrades_to_plan_id],
        remote_side=[id],
        backref="trial_plans"
    )
    trial_cooldown_days = Column(Integer, default=0, nullable=False)
    allow_auto_renewal = Column(Boolean, default=True, nullable=False)
    prodamus_subscription_id = Column(Integer, nullable=True, index=True)
    prodamus_autopayments_limit = Column(Integer, nullable=True)
    promo_codes = relationship("PromoCode", secondary=promocode_plan_association, back_populates="applicable_plans")


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'))
    amount = Column(Integer)
    currency = Column(String)
    provider = Column(String)
    status = Column(String)
    created_at = Column(DateTime, default=msk_now)


class UserSubscription(Base):
    __tablename__ = 'user_subscriptions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), unique=True, nullable=False)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True)
    start_date = Column(DateTime, default=msk_now)
    end_date = Column(DateTime, nullable=False)
    auto_renewal = Column(Boolean, default=True)
    payment_provider = Column(String)  # 'Prodamus', 'Telegram', 'Trial Welcome', 'Trial Promo'
    payment_method_id = Column(String, nullable=True)
    prodamus_subscription_id = Column(Integer, nullable=True, index=True)
    prodamus_profile_id = Column(Integer, nullable=True, index=True)
    last_payment_attempt = Column(DateTime, nullable=True)
    payment_attempt_count = Column(Integer, default=0, nullable=False)
    user = relationship("User", back_populates="subscription")
    plan = relationship("SubscriptionPlan")
    discount_percent = Column(Integer, default=0, nullable=False)


class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True, nullable=False)
    discount_percent = Column(Integer, default=0)
    free_days = Column(Integer, default=0)
    max_uses = Column(Integer, default=1)
    times_used = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    applies_to_all_plans = Column(Boolean, default=True, nullable=False)
    applicable_plans = relationship("SubscriptionPlan", secondary=promocode_plan_association,
                                    back_populates="promo_codes", lazy="selectin")
    users = relationship("User", secondary=user_promo_association, back_populates="promo_codes")


class SubscriptionConfig(Base):
    __tablename__ = 'subscription_config'
    id = Column(Integer, primary_key=True, default=1)
    prodamus_url = Column(String, nullable=True)  # URL формы Prodamus
    prodamus_secret_key = Column(String, nullable=True)  # Секретный ключ для подписи
    telegram_pay_token = Column(String, nullable=True)
    prodamus_paid_content = Column(Text, nullable=True)
    notifications_enabled = Column(Boolean, default=True)
    subscriptions_enabled = Column(Boolean, default=True, nullable=False)
    welcome_bonus_days = Column(Integer, default=0, nullable=False)
    referral_enabled = Column(Boolean, default=False, nullable=False)
    referral_bonus_days_referrer = Column(Integer, default=1, nullable=False)
    referral_bonus_days_referral = Column(Integer, default=1, nullable=False)
    referral_pay_bonus_enabled = Column(Boolean, default=False, nullable=False)
    referral_pay_bonus_days = Column(Integer, default=1, nullable=False)
    referral_pay_bonus_first_only = Column(Boolean, default=True, nullable=False)
    referral_reward_mode = Column(String, default="points", nullable=False)  # 'points' | 'bonus_days'
    referral_first_pay_percent = Column(Integer, default=20, nullable=False)
    referral_recurring_pay_percent = Column(Integer, default=10, nullable=False)


class ProdamusPayment(Base):
    __tablename__ = 'prodamus_payments'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=False)
    amount = Column(Float, nullable=False)
    order_id = Column(String, nullable=True, unique=True)
    prodamus_subscription_id = Column(Integer, nullable=True, index=True)
    event_type = Column(String, nullable=True)
    raw_payload = Column(Text, nullable=True)
    status = Column(String, default='pending', nullable=False)  # pending, completed, failed, canceled
    promo_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=msk_now)
    processed_at = Column(DateTime, nullable=True)


class TrialUsageHistory(Base):
    __tablename__ = 'trial_usage_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True, index=True)
    used_at = Column(DateTime, default=msk_now, nullable=False)


class ReferralPaymentLog(Base):
    __tablename__ = 'referral_payment_logs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, ForeignKey('users.id'), nullable=False, index=True)
    referred_user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    paid_at = Column(DateTime, default=msk_now)
    source_payment_key = Column(String, nullable=True, unique=True, index=True)


class PaymentIntent(Base):
    __tablename__ = 'payment_intents'
    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=False)
    gross_amount = Column(Float, nullable=False)
    promo_code_id = Column(Integer, ForeignKey('promo_codes.id', ondelete='SET NULL'), nullable=True)
    promo_discount_rub = Column(Float, default=0.0, nullable=False)
    points_reserved = Column(Integer, default=0, nullable=False)
    final_amount = Column(Float, nullable=False)
    provider = Column(String, nullable=False)  # 'telegram_pay', 'prodamus', 'balance'
    provider_payment_id = Column(String, nullable=True)
    status = Column(String, default='pending', nullable=False)  # pending, authorized, paid, cancelled, expired
    created_at = Column(DateTime, default=msk_now, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    pre_checkout_approved_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)


class Book(Base):
    __tablename__ = 'books'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    url = Column(String, nullable=False)


class BotConfig(Base):
    __tablename__ = 'bot_config'
    id = Column(Integer, primary_key=True, default=1)
    main_first_reminder_hours = Column(Integer, default=24)
    main_next_reminder_hours = Column(Integer, default=48)
    main_reminders_enabled = Column(Boolean, default=False)
    webhook_enabled = Column(Boolean, default=False)
    webhook_secret = Column(String, default='')
    webhook_port = Column(Integer, default=8081)
    crisis_marker_protection_enabled = Column(Boolean, default=True, nullable=False)
    crisis_markers_json = Column(
        Text,
        default=json.dumps(DEFAULT_CRISIS_MARKERS, ensure_ascii=False),
        nullable=False,
    )


class FunnelConfig(Base):
    __tablename__ = 'funnel_config'
    id = Column(Integer, primary_key=True, default=1)
    page_size = Column(Integer, default=10)
    first_reminder_hours = Column(Integer, default=6)
    next_reminder_hours = Column(Integer, default=24)
    resume_after_hours = Column(Integer, default=24)
    reminder_intervals = Column(String, default="1,2,3,7,14", nullable=False)
    meditation_timeout_hours = Column(Integer, default=2, nullable=False)
    meditation_timeouts_enabled = Column(Boolean, default=True, nullable=False)
    nurture_enabled = Column(Boolean, default=True, nullable=False)
    remarketing_enabled = Column(Boolean, default=True, nullable=False)


class FunnelMeditation(Base):
    __tablename__ = 'funnel_meditations'
    id = Column(Integer, primary_key=True, autoincrement=True)
    sort_order = Column(Integer, default=1, nullable=False)
    duration_minutes = Column(Integer, default=10, nullable=False)
    title = Column(String, nullable=False)
    short_description = Column(Text, nullable=True)
    full_description = Column(Text, nullable=True)
    file_id = Column(String, nullable=True)
    file_type = Column(String, nullable=True)
    reserve_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=msk_now)


class FunnelState(Base):
    __tablename__ = 'funnel_states'
    user_id = Column(BigInteger, ForeignKey('users.id'), primary_key=True)
    stage = Column(String, default='idle', nullable=False)
    meditation_id = Column(Integer, ForeignKey('funnel_meditations.id'), nullable=True)
    is_active = Column(Boolean, default=False)
    reminder_count = Column(Integer, default=0)
    last_user_action_at = Column(DateTime, default=msk_now)
    stage_changed_at = Column(DateTime, default=msk_now)
    next_followup_at = Column(DateTime, nullable=True)
    last_ai_reply = Column(Text, nullable=True)
    started_at = Column(DateTime, default=msk_now)
    completed_at = Column(DateTime, nullable=True)
    followup_sequence_anchor = Column(DateTime, nullable=True)
    stage_timeout_at = Column(DateTime, nullable=True)
    stage_timeout_sent = Column(Boolean, default=False, nullable=False)


class FunnelEvent(Base):
    __tablename__ = 'funnel_events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False)
    meditation_id = Column(Integer, ForeignKey('funnel_meditations.id'), nullable=True)
    stage = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now)


class FunnelLanding(Base):
    __tablename__ = 'funnel_landings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String, unique=True, nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), nullable=False, index=True)
    meditation_id = Column(Integer, ForeignKey('funnel_meditations.id'), nullable=True)
    origin_stage = Column(String(64), nullable=True)
    amount = Column(Integer, nullable=True)
    currency = Column(String, nullable=True)
    status = Column(String, default='opened', nullable=False)
    landing_url = Column(Text, nullable=True)
    external_payment_id = Column(String, nullable=True)
    customer_name = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    opened_at = Column(DateTime, default=msk_now, nullable=False)
    paid_at = Column(DateTime, nullable=True)
    form_notified = Column(Boolean, default=False, nullable=False)
    paid_notified = Column(Boolean, default=False, nullable=False)
    last_payload = Column(Text, nullable=True)


class LandingNotificationClaim(Base):
    __tablename__ = 'landing_notification_claims'
    id = Column(Integer, primary_key=True, autoincrement=True)
    landing_id = Column(Integer, ForeignKey('funnel_landings.id'), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    recipient_type = Column(String, default='admin', nullable=False)  # 'customer', 'admin'
    recipient_id = Column(BigInteger, default=0, nullable=False)
    status = Column(String, default='claimed', nullable=False)  # 'claimed', 'delivered'
    claimed_at = Column(DateTime, default=msk_now, nullable=False)
    delivered_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint('landing_id', 'event_type', 'recipient_type', 'recipient_id', name='uq_landing_event_recipient_claim'),
    )


class AiDisclaimerClaim(Base):
    __tablename__ = 'ai_disclaimer_claims'
    user_id = Column(BigInteger, ForeignKey('users.id'), primary_key=True)
    status = Column(String, default='claimed', nullable=False)  # 'claimed', 'delivered'
    claimed_at = Column(DateTime, default=msk_now, nullable=False)
    delivered_at = Column(DateTime, nullable=True)


class TestSession(Base):
    __test__ = False
    __tablename__ = 'test_sessions'
    user_id = Column(BigInteger, ForeignKey('users.id'), primary_key=True)
    current_question_index = Column(Integer, default=0, nullable=False)
    answers = Column(Text, default='[]', nullable=False)
    formula_results = Column(Text, nullable=True)
    invocation_topic_slug = Column(String, nullable=True)
    invocation_dialogue_id = Column(Integer, nullable=True)
    is_finished = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=msk_now, nullable=False)
    test_set_id = Column(Integer, nullable=True)
    topic_slug = Column(String, nullable=True)
    attempt_id = Column(Integer, ForeignKey('test_attempts.id', ondelete='SET NULL'), nullable=True, index=True)


class TestQuestion(Base):
    __test__ = False
    __tablename__ = 'test_questions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)
    category = Column(String, default='general', nullable=False)
    is_reverse = Column(Boolean, default=False, nullable=False)
    order_num = Column(Integer, default=0, nullable=False)
    sort_order = synonym('order_num')
    comment = Column(Text, nullable=True)
    variable_name = Column(String, nullable=True)
    allow_custom_answer = Column(Boolean, default=False, nullable=False)
    buttons_layout = Column(String, default='vertical', nullable=False)
    answer_options_json = Column(Text, nullable=True)
    question_type = Column(String(32), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    test_set_id = Column(Integer, ForeignKey('test_sets.id', ondelete='CASCADE'), nullable=True, index=True)
    test_set = relationship("TestSet", back_populates="questions")


class TestConfig(Base):
    """Deprecated: read-only compatibility facade. All active configurations use TestSet."""
    __test__ = False
    __tablename__ = 'test_config'
    id = Column(Integer, primary_key=True, default=1)
    is_enabled = Column(Boolean, default=True, nullable=False)
    show_progress = Column(Boolean, default=True, nullable=False)
    formulas_enabled = Column(Boolean, default=False, nullable=False)
    formulas_json = Column(Text, nullable=True)
    separate_result_prompt_enabled = Column(Boolean, default=False, nullable=False)
    result_system_prompt = Column(Text, nullable=True)
    interpretation_input_mode = Column(String, default='all', nullable=False)
    interpretation_selected_variables = Column(Text, nullable=True)


class TestSet(Base):
    __test__ = False
    __tablename__ = 'test_sets'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    name = synonym('title')
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_enabled = Column(Boolean, default=True, nullable=False)
    is_archived = Column(Boolean, default=False, nullable=False)
    show_progress = Column(Boolean, default=True, nullable=False)
    calculation_enabled = Column(Boolean, default=False, nullable=False)
    formulas_enabled = synonym('calculation_enabled')
    formulas_json = Column(Text, nullable=True)
    separate_interpretation_prompt = Column(Boolean, default=False, nullable=False)
    separate_result_prompt_enabled = synonym('separate_interpretation_prompt')
    interpretation_prompt = Column(Text, nullable=True)
    result_system_prompt = synonym('interpretation_prompt')
    system_prompt = Column(Text, nullable=True)
    preliminary_results_mode = Column(String, default='simple', nullable=False)
    handoff_mode = Column(String, default='prompt', nullable=False)
    interpretation_input_mode = Column(String, default='all', nullable=False)
    interpretation_selected_variables = Column(Text, nullable=True)
    expected_question_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)

    questions = relationship("TestQuestion", back_populates="test_set", cascade="all, delete-orphan", order_by="TestQuestion.order_num")


class TestAttempt(Base):
    __test__ = False
    __tablename__ = 'test_attempts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    test_set_id = Column(Integer, ForeignKey('test_sets.id', ondelete="SET NULL"), nullable=True, index=True)
    test_set_name = Column(String, nullable=True)
    test_set_name_snapshot = synonym('test_set_name')
    test_set_slug = Column(String, nullable=True)
    test_set_slug_snapshot = synonym('test_set_slug')
    topic_slug = Column(String, nullable=True, index=True)
    dialogue_id = Column(Integer, nullable=True)
    purpose = Column(String, default='general', nullable=False)
    answers = Column(Text, default='[]', nullable=False)
    formula_results = Column(Text, nullable=True)
    is_completed = Column(Boolean, default=False, nullable=False)
    started_at = Column(DateTime, default=msk_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String, default='in_progress', nullable=False)  # 'in_progress', 'completed', 'abandoned'
    # Immutable effective TestSet configuration captured when the attempt starts.
    # Nullable for compatibility with historical attempts created before v3 snapshots.
    configuration_snapshot_json = Column(Text, nullable=True)

    test_set = relationship("TestSet")


class PreTestClaim(Base):
    __tablename__ = 'pre_test_claims'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    topic_slug = Column(String, nullable=False, index=True)
    dialogue_id = Column(Integer, nullable=False, index=True)
    test_set_id = Column(Integer, nullable=False, index=True)
    attempt_id = Column(Integer, ForeignKey('test_attempts.id', ondelete='CASCADE'), nullable=True)
    status = Column(String, default='in_progress', nullable=False)  # 'in_progress', 'completed', 'failed', 'cancelled'
    claimed_at = Column(DateTime, default=msk_now, nullable=False)
    lease_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'topic_slug', 'dialogue_id', 'test_set_id', name='uq_pre_test_claim_identity'),
    )


class TopicPrerecordedDelivery(Base):
    __tablename__ = 'topic_prerecorded_deliveries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    topic_slug = Column(String, nullable=False, index=True)
    dialogue_id = Column(Integer, nullable=False, index=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='SET NULL'), nullable=True)
    status = Column(String, default='claimed', nullable=False)  # 'claimed', 'delivered', 'failed'
    claimed_at = Column(DateTime, default=msk_now, nullable=False)
    delivered_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_id', 'topic_slug', 'dialogue_id', name='uq_topic_prerecorded_delivery'),
    )


class PendingAiFlow(Base):
    __tablename__ = 'pending_ai_flows'
    user_id = Column(BigInteger, primary_key=True)
    flow_type = Column(String, nullable=False)  # 'topic', 'diagnostics', 'thematic_session', 'funnel_step', 'funnel_objection'
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)

    @property
    def payload(self) -> dict:
        if self.payload_json:
            try:
                return json.loads(self.payload_json)
            except Exception:
                return {}
        return {}

    @payload.setter
    def payload(self, val: dict | None) -> None:
        self.payload_json = json.dumps(val, ensure_ascii=False) if val is not None else None


class TopicDialogueConfig(Base):
    __tablename__ = 'topic_dialogue_configs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    topic_slug = Column(String, ForeignKey('topics.slug', ondelete="CASCADE"), unique=True, nullable=False, index=True)
    welcome_text = Column(Text, nullable=True)
    welcome_media_type = Column(String, nullable=True)  # 'photo', 'video', 'document', 'animation'
    welcome_media_id = Column(String, nullable=True)
    reset_dialogue_on_entry = Column(Boolean, default=True, nullable=False)
    reset_context_on_entry = synonym('reset_dialogue_on_entry')
    start_dialogue_button_enabled = Column(Boolean, default=False, nullable=False)
    start_button_enabled = synonym('start_dialogue_button_enabled')
    start_dialogue_button_text = Column(String, default="Начать диалог", nullable=False)
    start_button_text = synonym('start_dialogue_button_text')
    prerecorded_first_message = Column(Text, nullable=True)
    pre_dialogue_test_enabled = Column(Boolean, default=False, nullable=False)
    test_enabled = synonym('pre_dialogue_test_enabled')
    test_set_id = Column(Integer, ForeignKey('test_sets.id', ondelete="SET NULL"), nullable=True)
    summary_generation_enabled = Column(Boolean, default=False, nullable=False)
    summary_enabled = synonym('summary_generation_enabled')
    summary_prompt = Column(Text, nullable=True)

    topic = relationship("Topic", backref=backref("dialogue_config", uselist=False, cascade="all, delete-orphan"))
    test_set = relationship("TestSet")


class UserTopicSession(Base):
    __tablename__ = 'user_topic_sessions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    topic_slug = Column(String, ForeignKey('topics.slug', ondelete="CASCADE"), nullable=False, index=True)
    active_dialogue_id = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'topic_slug', name='uq_user_topic_session'),
    )


class SessionSummary(Base):
    __tablename__ = 'session_summaries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete="CASCADE"), nullable=False, index=True)
    topic_slug = Column(String, ForeignKey('topics.slug', ondelete="SET NULL"), nullable=True, index=True)
    topic_slug_snapshot = Column(String, nullable=False)
    topic_name_snapshot = Column(String, nullable=True)
    dialogue_id = Column(Integer, nullable=False, index=True)
    summary_text = Column(Text, nullable=False)
    summary_prompt_used = Column(Text, nullable=True)
    version = Column(Integer, default=1, nullable=False)
    extra_metadata = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'topic_slug_snapshot', 'dialogue_id', 'version', name='uq_session_summary_version'),
        Index('ix_summary_lookup', 'user_id', 'topic_slug_snapshot', 'dialogue_id', 'version'),
    )


class SummaryClaim(Base):
    __tablename__ = 'summary_claims'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    topic_slug = Column(String, nullable=False, index=True)
    dialogue_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String, default='in_progress', nullable=False)  # 'in_progress', 'completed', 'failed'
    summary_id = Column(Integer, nullable=True)
    claimed_at = Column(DateTime, default=msk_now, nullable=False)
    lease_until = Column(DateTime, nullable=False)
    owner_token = Column(String, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_id', 'topic_slug', 'dialogue_id', 'version', name='uq_summary_claim_version'),
    )


# Platform architecture: AI structured logging, hardened mailing, and referral idempotency
class AILog(Base):
    __tablename__ = 'ai_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=True, index=True)
    dialogue_id = Column(Integer, nullable=True)
    topic_slug = Column(String, nullable=True, index=True)
    topic_id = Column(Integer, nullable=True)
    topic_name_snapshot = Column(String, nullable=True)
    platform = Column(String, default="telegram", nullable=True, index=True)
    context_kind = Column(String, default="main", nullable=True)
    request_type = Column(String, default='chat', nullable=False, index=True)
    provider = Column(String, nullable=False)
    model = Column(String, nullable=False)
    request_group_id = Column(String, nullable=True, index=True)
    attempt_no = Column(Integer, default=1, nullable=False)
    attempt_role = Column(String, default='primary', nullable=False)  # 'primary' | 'fallback'
    status = Column(String, default='success', nullable=False, index=True)  # 'success' | 'error'
    latency_ms = Column(Integer, nullable=True)
    prompt_summary = Column(Text, nullable=True)
    request_payload = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)
    clean_text = Column(Text, nullable=True)
    error_type = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    error_classification = Column(String, nullable=True)
    http_status = Column(Integer, nullable=True)
    finish_reason = Column(String, nullable=True)
    diagnostics_json = Column(Text, nullable=True)
    provider_response_payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class JournalConfig(Base):
    __tablename__ = 'journal_config'
    id = Column(Integer, primary_key=True, default=1)
    points_per_reflection = Column(Integer, default=10, nullable=False)
    questions_seeded = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=msk_now)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now)


class JournalSettings(Base):
    __tablename__ = 'journal_settings'
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True, index=True)
    reminders_enabled = Column(Boolean, default=False, nullable=False)
    reminder_time = Column(String(5), nullable=True)  # "HH:MM" in MSK
    last_reminded_date = Column(Date, nullable=True)
    snooze_until = Column(DateTime, nullable=True)
    snooze_claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=msk_now)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now)


class QuickNote(Base):
    __tablename__ = 'quick_notes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=msk_now, nullable=False, index=True)
    operation_key = Column(String(64), nullable=True, unique=True, index=True)


class JournalQuestion(Base):
    __tablename__ = 'journal_questions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)
    order_num = Column(Integer, default=0, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    question_type = Column(String(32), default='text', nullable=False)
    allow_custom_answer = Column(Boolean, default=False, nullable=False)
    answer_options_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now)


class JournalEntry(Base):
    __tablename__ = 'journal_entries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    reflection_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), default='in_progress', nullable=False)
    questions_snapshot_json = Column(Text, nullable=False)
    started_at = Column(DateTime, default=msk_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    summary_text = Column(Text, nullable=True)
    encouraging_message = Column(Text, nullable=True)
    tomorrow_focus = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    points_awarded = Column(Integer, default=0, nullable=False)
    points_awarded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=msk_now)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now)

    __table_args__ = (
        UniqueConstraint('user_id', 'reflection_date', name='uq_journal_entry_user_date'),
    )


class JournalAnswer(Base):
    __tablename__ = 'journal_answers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(Integer, ForeignKey('journal_entries.id', ondelete='CASCADE'), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey('journal_questions.id', ondelete='SET NULL'), nullable=True)
    position = Column(Integer, nullable=False)
    question_text_snapshot = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=True)
    is_skipped = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=msk_now)
    updated_at = Column(DateTime, default=msk_now, onupdate=msk_now)

    __table_args__ = (
        UniqueConstraint('entry_id', 'position', name='uq_journal_answer_entry_pos'),
    )


class JournalReward(Base):
    __tablename__ = 'journal_rewards'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    target_days = Column(Integer, default=21, nullable=False)
    cycle_number = Column(Integer, default=1, nullable=False)
    days_granted = Column(Integer, default=7, nullable=False)
    granted_at = Column(DateTime, default=msk_now, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'cycle_number', name='uq_journal_reward_user_cycle'),
    )


class JournalAiOperation(Base):
    __tablename__ = 'journal_ai_operations'
    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_key = Column(String(128), unique=True, index=True, nullable=False)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    entry_id = Column(Integer, ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True)
    operation_type = Column(String(32), nullable=False)  # daily_summary, summary_retry, dynamics_7, dynamics_21
    status = Column(String(20), default='pending', nullable=False)  # pending, running, succeeded, failed
    token_cost = Column(Float, default=0.0, nullable=False)
    owner_token = Column(String(64), nullable=True)
    started_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    charged_at = Column(DateTime, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    result_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=msk_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)


from mailing_service import MailingDeliveryLog
from referral_service import ReferralRewardGrant
from automation_engine import (
    AutomationConversationState,
    AutomationStepTransition,
    AutomationHandler,
    AutomationActionExecution,
)
from followups import FollowupCampaign, FollowupStep, FollowupRun, FollowupDelivery


async def get_all_admin_ids() -> set:
    from config import OWNER_IDS
    all_ids = set(OWNER_IDS)
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(User.id).where(User.is_admin == True))
            all_ids.update({row[0] for row in result})
    except Exception:
        pass
    return all_ids


def _sql_default_literal(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


async def _sqlite_table_columns(conn, table_name: str) -> set[str]:
    rows = (await conn.execute(text(f'PRAGMA table_info("{table_name}")'))).fetchall()
    return {row[1] for row in rows}


async def _sqlite_table_info(conn, table_name: str) -> list[dict]:
    rows = (await conn.execute(text(f'PRAGMA table_info("{table_name}")'))).fetchall()
    return [
        {
            "cid": row[0],
            "name": row[1],
            "type": row[2] or "",
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": row[5],
        }
        for row in rows
    ]


async def _sqlite_rebuild_table_to_model(conn, table, *, select_map: dict[str, str] | None = None):
    select_map = select_map or {}
    source_columns = {col["name"] for col in await _sqlite_table_info(conn, table.name)}

    temp_name = f"__tmp_{table.name}"
    temp_table = table.to_metadata(table.metadata, name=temp_name)
    temp_table.indexes.clear()

    await conn.execute(text(f'DROP TABLE IF EXISTS "{temp_name}"'))
    try:
        await conn.run_sync(lambda sync_conn: temp_table.create(sync_conn))

        insert_columns = []
        select_expressions = []
        for column in table.columns:
            if column.name in select_map:
                insert_columns.append(f'"{column.name}"')
                select_expressions.append(f"{select_map[column.name]} AS \"{column.name}\"")
            elif column.name in source_columns:
                insert_columns.append(f'"{column.name}"')
                select_expressions.append(f'"{column.name}"')

        if insert_columns:
            await conn.execute(text(
                f'INSERT INTO "{temp_name}" ({", ".join(insert_columns)}) '
                f'SELECT {", ".join(select_expressions)} FROM "{table.name}"'
            ))

        await conn.execute(text(f'DROP TABLE "{table.name}"'))
        await conn.execute(text(f'ALTER TABLE "{temp_name}" RENAME TO "{table.name}"'))
        await conn.run_sync(lambda sync_conn: [index.create(sync_conn, checkfirst=True) for index in table.indexes])
    finally:
        if temp_table.name in table.metadata.tables:
            table.metadata.remove(temp_table)


async def _ensure_sqlite_compat_schema(conn):
    if conn.dialect.name != "sqlite":
        return
    dialect = conn.dialect

    # 1. Explicit pre-v6 migration for test_questions:
    # Preserves legacy sort_order -> order_num without default 0 zeroing,
    # backfills test_set_id -> 1, and creates foreign key to test_sets(id).
    existing_q_cols = await _sqlite_table_columns(conn, "test_questions")
    if existing_q_cols and ("sort_order" in existing_q_cols or "test_set_id" not in existing_q_cols or "order_num" not in existing_q_cols):
        order_num_expr = '"sort_order"' if "sort_order" in existing_q_cols else 'COALESCE("order_num", 0)'
        test_set_expr = 'COALESCE("test_set_id", 1)' if "test_set_id" in existing_q_cols else '1'
        await _sqlite_rebuild_table_to_model(
            conn,
            TestQuestion.__table__,
            select_map={
                "order_num": order_num_expr,
                "test_set_id": test_set_expr,
                "is_active": "1",
                "question_type": "NULL",
            },
        )

    # 2. Explicit pre-v6 migration for test_sessions:
    # Backfills test_set_id -> 1, adds attempt_id, and establishes foreign keys.
    existing_sess_cols = await _sqlite_table_columns(conn, "test_sessions")
    if existing_sess_cols and ("test_set_id" not in existing_sess_cols or "attempt_id" not in existing_sess_cols):
        test_set_expr = 'COALESCE("test_set_id", 1)' if "test_set_id" in existing_sess_cols else '1'
        await _sqlite_rebuild_table_to_model(
            conn,
            TestSession.__table__,
            select_map={
                "test_set_id": test_set_expr,
            },
        )

    # 3. Explicit migration for subscription_plans:
    existing_plan_columns = await _sqlite_table_columns(conn, "subscription_plans")
    if "duration_days" in existing_plan_columns:
        price_expr = '"price"'
        if "duration_value" not in existing_plan_columns:
            price_expr = 'ROUND("price" / 100.0, 2)'

        await _sqlite_rebuild_table_to_model(
            conn,
            SubscriptionPlan.__table__,
            select_map={
                "price": price_expr,
                "duration_value": 'COALESCE("duration_value", "duration_days", 30)',
                "duration_unit": 'COALESCE(NULLIF("duration_unit", \'\'), \'days\')',
            },
        )

    # 4. Explicit pre-v6 migration for messages:
    # Preserves legacy messages, timestamps, topic/dialogue, and establishes foreign key
    # to test_attempts(id) ON DELETE SET NULL and its index.
    existing_msg_cols = await _sqlite_table_columns(conn, "messages")
    if existing_msg_cols:
        fks_res = await conn.execute(text("PRAGMA foreign_key_list(messages)"))
        fks = fks_res.fetchall()
        has_test_attempt_fk = any(
            row[2] == "test_attempts" and row[3] == "test_attempt_id"
            for row in fks
        )
        if not has_test_attempt_fk or "test_attempt_id" not in existing_msg_cols:
            select_map = {}
            if "created_at" in existing_msg_cols and "timestamp" not in existing_msg_cols:
                select_map["timestamp"] = '"created_at"'
            elif "created_at" in existing_msg_cols and "timestamp" in existing_msg_cols:
                select_map["timestamp"] = 'COALESCE("timestamp", "created_at")'
            if "dialogue_id" not in existing_msg_cols:
                select_map["dialogue_id"] = '1'
            if "message_kind" not in existing_msg_cols:
                select_map["message_kind"] = "'standard'"

            await _sqlite_rebuild_table_to_model(
                conn,
                Message.__table__,
                select_map=select_map,
            )

    # 5. Explicit migration for users.journal_points:
    existing_user_cols = await _sqlite_table_columns(conn, "users")
    if existing_user_cols and "journal_points" not in existing_user_cols:
        await conn.execute(text('ALTER TABLE "users" ADD COLUMN "journal_points" INTEGER NOT NULL DEFAULT 0'))

    if existing_user_cols and "admin_role" not in existing_user_cols:
        await conn.execute(text('ALTER TABLE "users" ADD COLUMN "admin_role" VARCHAR(32) NULL'))

    existing_bot_config_cols = await _sqlite_table_columns(conn, "bot_config")
    if existing_bot_config_cols and "crisis_marker_protection_enabled" not in existing_bot_config_cols:
        await conn.execute(text(
            'ALTER TABLE "bot_config" ADD COLUMN "crisis_marker_protection_enabled" BOOLEAN NOT NULL DEFAULT 1'
        ))
    if existing_bot_config_cols and "crisis_markers_json" not in existing_bot_config_cols:
        default_markers = json.dumps(DEFAULT_CRISIS_MARKERS, ensure_ascii=False).replace("'", "''")
        await conn.execute(text(
            f'ALTER TABLE "bot_config" ADD COLUMN "crisis_markers_json" TEXT NOT NULL DEFAULT \'{default_markers}\''
        ))

    for table in Base.metadata.sorted_tables:
        existing_columns = await _sqlite_table_columns(conn, table.name)
        if not existing_columns:
            continue

        for column in table.columns:
            if column.name in existing_columns:
                continue

            column_type = column.type.compile(dialect=dialect)
            stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'

            default_literal = None
            if column.default is not None and getattr(column.default, "is_scalar", False):
                default_literal = _sql_default_literal(column.default.arg)
            elif not column.nullable:
                if isinstance(column.type, Boolean):
                    default_literal = "0"
                elif isinstance(column.type, Integer):
                    default_literal = "0"
                elif isinstance(column.type, Float):
                    default_literal = "0"
                elif isinstance(column.type, String):
                    default_literal = "''"

            if default_literal is not None:
                stmt += f" DEFAULT {default_literal}"

            await conn.execute(text(stmt))

        if table.name == "menu_buttons":
            await conn.execute(text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY section_key, source_type, source_id
                               ORDER BY CASE WHEN enabled = 1 THEN 0 ELSE 1 END, id
                           ) AS row_num
                    FROM menu_buttons
                    WHERE source_type = 'book' AND source_id IS NOT NULL
                )
                UPDATE menu_buttons
                SET enabled = 0
                WHERE id IN (SELECT id FROM ranked WHERE row_num > 1)
                """
            ))

        for index in table.indexes:
            try:
                await conn.run_sync(lambda sync_conn, idx=index: idx.create(sync_conn, checkfirst=True))
            except Exception:
                pass

    if existing_user_cols:
        await conn.execute(text(
            'UPDATE "users" SET "admin_role" = \'full_admin\' '
            'WHERE "is_admin" = 1 AND ("admin_role" IS NULL OR TRIM("admin_role") = \'\')'
        ))
        await conn.execute(text(
            'UPDATE "users" '
            'SET "welcome_shown_at" = COALESCE("welcome_shown_at", "created_at", CURRENT_TIMESTAMP) '
            'WHERE "welcome_shown_at" IS NULL'
        ))

    for table in Base.metadata.sorted_tables:
        existing_info = await _sqlite_table_info(conn, table.name)
        if not existing_info:
            continue

        model_columns = {column.name for column in table.columns}
        legacy_required_columns = [
            column["name"]
            for column in existing_info
            if column["name"] not in model_columns and column["notnull"] and column["default"] is None
        ]
        if legacy_required_columns:
            await _sqlite_rebuild_table_to_model(conn, table)


async def _ensure_postgresql_compat_schema(conn):
    if conn.dialect.name != "postgresql":
        return
    await conn.execute(text(
        'ALTER TABLE "admin_navigation_contexts" ADD COLUMN IF NOT EXISTS '
        '"parent_callback" VARCHAR(255) NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "test_questions" ADD COLUMN IF NOT EXISTS "question_type" VARCHAR(32) NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "test_attempts" ADD COLUMN IF NOT EXISTS "configuration_snapshot_json" TEXT NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "test_questions" ADD COLUMN IF NOT EXISTS "is_active" BOOLEAN NOT NULL DEFAULT TRUE'
    ))
    await conn.execute(text(
        'ALTER TABLE "test_sets" ADD COLUMN IF NOT EXISTS "expected_question_count" INTEGER NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "journal_questions" ADD COLUMN IF NOT EXISTS "question_type" VARCHAR(32) NOT NULL DEFAULT \'text\''
    ))
    await conn.execute(text(
        'ALTER TABLE "journal_questions" ADD COLUMN IF NOT EXISTS "allow_custom_answer" BOOLEAN NOT NULL DEFAULT FALSE'
    ))
    await conn.execute(text(
        'ALTER TABLE "journal_questions" ADD COLUMN IF NOT EXISTS "answer_options_json" TEXT NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "journal_answers" ADD COLUMN IF NOT EXISTS "is_skipped" BOOLEAN NOT NULL DEFAULT FALSE'
    ))
    await conn.execute(text(
        'ALTER TABLE "quick_notes" ADD COLUMN IF NOT EXISTS "operation_key" VARCHAR(64) NULL'
    ))
    await conn.execute(text(
        'CREATE UNIQUE INDEX IF NOT EXISTS "ix_quick_notes_operation_key" '
        'ON "quick_notes" ("operation_key") WHERE "operation_key" IS NOT NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "welcome_shown_at" TIMESTAMP NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "crisis_detected_at" TIMESTAMP NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "habit_timezone" VARCHAR(64) NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "admin_role" VARCHAR(32) NULL'
    ))
    default_markers = json.dumps(DEFAULT_CRISIS_MARKERS, ensure_ascii=False).replace("'", "''")
    await conn.execute(text(
        'ALTER TABLE "bot_config" ADD COLUMN IF NOT EXISTS '
        '"crisis_marker_protection_enabled" BOOLEAN NOT NULL DEFAULT TRUE'
    ))
    await conn.execute(text(
        f'ALTER TABLE "bot_config" ADD COLUMN IF NOT EXISTS "crisis_markers_json" '
        f"TEXT NOT NULL DEFAULT '{default_markers}'"
    ))
    await conn.execute(text(
        'UPDATE "users" SET "admin_role" = \'full_admin\' '
        'WHERE "is_admin" = TRUE AND ("admin_role" IS NULL OR TRIM("admin_role") = \'\')'
    ))
    await conn.execute(text(
        'ALTER TABLE "automation_handlers" ADD COLUMN IF NOT EXISTS "recipient_ids_json" TEXT NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "funnel_landings" ADD COLUMN IF NOT EXISTS "origin_stage" VARCHAR(64) NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "funnel_config" ADD COLUMN IF NOT EXISTS "meditation_timeouts_enabled" BOOLEAN NOT NULL DEFAULT TRUE'
    ))
    await conn.execute(text(
        'ALTER TABLE "funnel_config" ADD COLUMN IF NOT EXISTS "nurture_enabled" BOOLEAN NOT NULL DEFAULT TRUE'
    ))
    await conn.execute(text(
        'ALTER TABLE "funnel_config" ADD COLUMN IF NOT EXISTS "remarketing_enabled" BOOLEAN NOT NULL DEFAULT TRUE'
    ))
    await conn.execute(text(
        'ALTER TABLE "habits" ADD COLUMN IF NOT EXISTS "next_reminder_at" TIMESTAMP NULL'
    ))
    await conn.execute(text(
        'ALTER TABLE "habits" ADD COLUMN IF NOT EXISTS "last_reminder_at" TIMESTAMP NULL'
    ))
    await conn.execute(text(
        'UPDATE "funnel_config" '
        'SET "meditation_timeouts_enabled" = COALESCE("meditation_timeouts_enabled", TRUE), '
        '"nurture_enabled" = COALESCE("nurture_enabled", TRUE), '
        '"remarketing_enabled" = COALESCE("remarketing_enabled", TRUE)'
    ))
    await conn.execute(text(
        'UPDATE "users" '
        'SET "welcome_shown_at" = COALESCE("welcome_shown_at", "created_at", CURRENT_TIMESTAMP) '
        'WHERE "welcome_shown_at" IS NULL'
    ))


CANONICAL_QUESTIONS = [
    "Что сегодня запомнилось сильнее всего?",
    "Какие чувства возникали чаще всего в течение этого дня?",
    "Что именно вызывало у тебя чувства благодарности, беспокойства, радости и замешательства?",
    "Чему тебя сегодня научил день?",
    "Что ты хочешь сохранить в своей жизни, а что отпустить?",
    "Что ты выбираешь сделать завтра иначе?",
]

DIAGNOSTIC_AI_SESSION_SET_SLUG = "diagnostics"
PSYCHOLOGICAL_PROFILE_SET_SLUG = "express_psychological_profile"


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_sqlite_compat_schema(conn)
        await _ensure_postgresql_compat_schema(conn)

    async with async_session_maker() as session:
        if not await session.get(AIConfig, 1):
            session.add(AIConfig(id=1))

        if not await session.get(ReferralConfig, 1):
            session.add(ReferralConfig(id=1))

        if not await session.get(HabitConfig, 1):
            session.add(HabitConfig(id=1, mode='hybrid', streak_days=7, reward_sequence="М,О,Л,О,Д,Е,Ц",
                                    time_window_minutes=60))

        if not await session.get(BotConfig, 1):
            session.add(BotConfig(id=1))

        if not await session.get(PaymentConfig, 1):
            session.add(PaymentConfig(id=1, mode='free'))

        if not await session.get(SubscriptionConfig, 1):
            session.add(SubscriptionConfig(id=1))

        if not await session.get(TestConfig, 1):
            session.add(TestConfig(id=1))

        default_topics = [
            ("free_session", "AI Свободная сессия", None),
            ("diagnostics", "AI Диагностика + сессия", None),
            ("express", "AI Экспресс диагностика", None),
            ("journal", "Дневник саморефлексии", None),
            ("habits", "Формировать привычки", None),
            (
                "funnel_demo",
                "ИИ-разбор гипносессии",
                (
                    "Ты ИИ-ассистент психолога Виталия Наумова. "
                    "Дай бережный, глубокий и конкретный разбор опыта после медитативной практики. "
                    "Структура ответа: 1) что откликнулось в опыте пользователя, "
                    "2) какие паттерны или внутренние динамики можно заметить, "
                    "3) короткий персональный инсайт, "
                    "4) мягкий мостик к стратегической сессии без давления. "
                    "Не ставь диагнозы и не используй медицинские обещания."
                ),
            ),
            (
                "strategy_session",
                "Стратегическая сессия",
                (
                    "Ты ИИ-ассистент психолога Виталия Наумова. "
                    "Веди мягкий диалог, который помогает человеку понять, нужна ли ему стратегическая сессия. "
                    "В начале уточни, знаком ли человек с Виталием и методом Транскоучинг. "
                    "Задавай по одному вопросу, коротко резюмируй ответы и подводи к осознанной записи на сессию, "
                    "если видишь запрос. Не дави и не обещай чудес."
                ),
            ),
            (
                "strategy_objections",
                "Возражения перед стратегической сессией",
                (
                    "Ты ИИ-ассистент психолога Виталия Наумова. "
                    "Помоги человеку бережно исследовать, что именно его останавливает перед стратегической сессией. "
                    "Не спорь и не дави. Сначала отрази смысл сказанного, затем покажи возможный внутренний конфликт, "
                    "после этого задай один уточняющий вопрос или предложи один следующий шаг. "
                    "Если уместно, мягко подведи к записи на стратегическую сессию."
                ),
            ),
            (
                "stress",
                "Стресс и выгорание",
                (
                    "Ты — эмпатичный ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай пользователю исследовать причины стресса и хронической усталости, "
                    "находить ресурсные состояния и бережные практики саморегуляции."
                ),
            ),
            (
                "self_esteem",
                "Самооценка и синдром самозванца",
                (
                    "Ты — поддерживающий ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай пользователю исследовать внутреннюю критику, "
                    "укреплять контакт с собственной ценностью и присваивать свои достижения."
                ),
            ),
            (
                "relationships",
                "Отношения и границы",
                (
                    "Ты — внимательный ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай исследовать сложности в отношениях, личные границы, "
                    "сценарии созависимости и экологичную коммуникацию."
                ),
            ),
            (
                "sleep_recovery",
                "Сон и восстановление",
                (
                    "Ты — заботливый ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай выявлять факторы, мешающие качественному сну, "
                    "нормализовать вечерний режим и находить техники релаксации."
                ),
            ),
            (
                "motivation_goals",
                "Мотивация и цели",
                (
                    "Ты — вдохновляющий ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай отделять навязанные цели от истинных, "
                    "выявлять саботаж и находить внутренний импульс к действию."
                ),
            ),
            (
                "emotional_intelligence",
                "Эмоциональный интеллект",
                (
                    "Ты — чуткий ИИ-ассистент психолога Виталия Наумова. "
                    "Помогай распознавать, называть и проживать сложные эмоции "
                    "(злость, стыд, страх, вину) без саморазрушения."
                ),
            ),
        ]

        for slug, name, prompt in default_topics:
            stmt = await session.execute(
                text("SELECT 1 FROM topics WHERE slug = :slug"), {"slug": slug}
            )
            if not stmt.scalar():
                session.add(Topic(slug=slug, name=name, system_prompt=prompt))

        default_content = [
            ("start_msg", "Приветственное сообщение", None),
            ("disclaimer_text", "<i>Бот не заменяет консультацию специалиста. Если ты в кризисной ситуации, обратись к профессионалу.</i>", None),
            ("habits_text", "Текст раздела 'Формировать привычки'", None),
            ("journal_intro", "📔 Дневник саморефлексии\n\nЗапиши, что сегодня было важным, и заметь изменения.", None),
            ("closed_club_intro", "Текст 'Закрытый клуб'", None),
            ("referral_intro", "Текст 'Реферальная программа'", None),
            ("referral_link_text", "🎁 Получить бонус (Твоя ссылка)", None),
            ("vitaly_intro", "Текст 'Виталий Наумов'", None),
            ("books_intro", "Текст раздела 'Книги'", None),
            ("extended_menu_intro", "Выбери раздел:", None),
            ("thematic_sessions_intro", "Выбери тему для диалога:", None),
            ("subscription_info", "Текст 'Информация о подписке'", None),
            ("btn_club_materials", "📚 Полезные материалы", "https://example.com"),
            ("btn_club_articles", "📰 Полезные статьи", "https://example.com"),
            ("btn_club_chat", "💬 Присоединиться к чату", "https://t.me/+"),
            ("btn_club_session", "📅 Записаться на сессию", "https://webapp.link"),
            ("btn_club_question", "❓ Задать вопрос", "https://t.me/username"),
            ("btn_club_masterclass", "🎭 Афиша мастер классов", "https://example.com"),
            ("btn_vitaly_consult", "📅 Записаться на консультацию", "https://webapp.link"),
            ("btn_vitaly_speaker", "🎤 Спикерство / Пригласить", "https://webapp.link"),
            ("btn_book_session_main", "📅 Запись на сессию (Главное меню)", "https://webapp.link"),
            ("btn_academy", "🎓 Академия Транскоучинг", "https://webapp.link"),
            ("btn_rich_thinking", "💎 Богатое мышление", "https://t.me/channel"),
            ("btn_transfo", "🦋 Трансформации", "https://t.me/channel"),
            ("btn_book_1", "Книга 1 (Название)", "https://example.com"),
            ("btn_book_2", "Книга 2 (Название)", "https://example.com"),
            ("btn_book_3", "Книга 3 (Название)", "https://example.com"),
            ("btn_book_4", "Книга 4 (Название)", "https://example.com"),
            ("btn_book_5", "Книга 5 (Название)", "https://example.com"),
            ("diagnostics_questions", "1. Как вы себя чувствуете?\n2. Что вас беспокоит?", None),
            (
                "funnel_intro",
                (
                    "Привет!\n\n"
                    "Я — ИИ-ассистент психолога Виталия Наумова, автора метода Транскоучинг.\n\n"
                    "Здесь ты получишь:\n"
                    "🎧 Медитативную гипносессию\n"
                    "🔮 Персональный разбор образов и ощущений\n"
                    "💡 Инсайт о внутренних процессах\n\n"
                    "Это займёт 15-20 минут."
                ),
                None,
            ),
            (
                "funnel_about_method",
                (
                    "Транскоучинг — это бережная работа с внутренними образами, состояниями и повторяющимися "
                    "жизненными сценариями. Сначала ты получаешь практику, затем делишься ощущениями, а я помогаю "
                    "увидеть важные сигналы и связать их с твоим запросом."
                ),
                None,
            ),
            (
                "funnel_choice_intro",
                "Выбери практику, которая откликается сейчас больше всего. После выбора я покажу полное описание и кнопку запуска.",
                None,
            ),
            (
                "funnel_listen_instruction",
                (
                    "Перед началом:\n"
                    "✅ Найди тихое место\n"
                    "✅ Сядь или ляг удобно\n"
                    "✅ По возможности используй наушники\n"
                    "✅ Просто слушай и следуй за голосом\n\n"
                    "После практики вернись и опиши, что увидел(а) или почувствовал(а)."
                ),
                None,
            ),
            (
                "funnel_reflection_prompt",
                (
                    "Как прошла практика?\n\n"
                    "Опиши свободно, что ты увидел(а) или почувствовал(а):\n"
                    "• какие образы приходили,\n"
                    "• какие были ощущения в теле,\n"
                    "• какие эмоции поднимались,\n"
                    "• были ли воспоминания или неожиданные мысли.\n\n"
                    "Лучше 2-3 предложения или больше."
                ),
                None,
            ),
            (
                "funnel_strategy_intro",
                (
                    "Виталий Наумов — психолог и автор метода Транскоучинг.\n\n"
                    "Стратегическая сессия помогает увидеть корневые внутренние динамики, понять, где именно "
                    "ты буксуешь, и наметить следующий шаг в работе с собой."
                ),
                None,
            ),
            (
                "funnel_strategy_offer",
                (
                    "Если откликнулся этот разбор, следующим шагом может стать стратегическая сессия с Виталием.\n\n"
                    "На ней вы глубже разберёте запрос, увидите ограничения и определите точку роста."
                ),
                None,
            ),
            (
                "funnel_after_analysis_offer",
                (
                    "Это лишь верхушка айсберга.\n\n"
                    "То, что ты увидел(а) в практике — только начало.\n"
                    "На полной стратегической сессии с Виталием ты:\n"
                    "• пройдёшь глубокую диагностику по методу Транскоучинг,\n"
                    "• выявишь корневые причины текущих сложностей,\n"
                    "• получишь персональную карту трансформации,\n"
                    "• поймёшь конкретные шаги, что делать дальше.\n\n"
                    "Хочешь узнать больше о стратегической сессии?"
                ),
                None,
            ),
            (
                "funnel_vitaly_method",
                (
                    "🙌 ВИТАЛИЙ НАУМОВ\n"
                    "Психолог, коуч-эриксонианец с 1998 года.\n"
                    "Создатель метода Транскоучинг — синтеза эриксоновского гипноза, коучинга и глубинной психологии.\n\n"
                    "🎯 ЧТО ТАКОЕ ТРАНСКОУЧИНГ\n"
                    "Это метод, который работает не с симптомами, а с причинами.\n"
                    "Мы находим бессознательные паттерны и убеждения, которые управляют жизнью — и трансформируем их.\n\n"
                    "Результат:\n"
                    "✅ Выход из застревания\n"
                    "✅ Новые возможности\n"
                    "✅ Реализация потенциала\n"
                    "✅ Жизнь как миссия"
                ),
                None,
            ),
            (
                "funnel_strategy_sales",
                (
                    "🔥 СТРАТЕГИЧЕСКАЯ КОУЧ-СЕССИЯ С ВИТАЛИЕМ\n\n"
                    "Это 2-часовая онлайн-сессия, где ты:\n"
                    "1. Пройдёшь глубокую диагностику\n"
                    "2. Получишь персональную карту трансформации\n"
                    "3. Узнаешь конкретные шаги\n"
                    "4. Сделаешь первый прорыв\n\n"
                    "💰 Стоимость: 1500 руб\n"
                    "Это диагностическая сессия, на которой ты поймёшь, подходит ли тебе метод.\n\n"
                    "Кому особенно откликается:\n"
                    "• если чувствуешь, что застрял(а),\n"
                    "• если повторяются одни и те же паттерны,\n"
                    "• если хочешь понять своё предназначение,\n"
                    "• если есть внутренний тормоз,\n"
                    "• если чувствуешь, что способен(на) на большее."
                ),
                None,
            ),
            (
                "funnel_strategy_faq",
                (
                    "Частые вопросы о стратегической сессии:\n\n"
                    "1. А вдруг мне не подойдёт?\n"
                    "Стратегическая сессия как раз помогает это проверить на практике.\n\n"
                    "2. Я не готов(а) к изменениям.\n"
                    "Это нормально. На сессии мы не ломаем сопротивление, а понимаем, что оно защищает.\n\n"
                    "3. У меня нет времени.\n"
                    "Иногда 2 часа сейчас экономят месяцы и годы блужданий.\n\n"
                    "4. Это дорого.\n"
                    "1500 руб — это формат входной диагностики, чтобы ты понял(а), есть ли ценность именно для тебя.\n\n"
                    "5. Что будет после сессии?\n"
                    "Ты либо уйдёшь с инсайтами и пониманием, либо при желании продолжишь работу дальше."
                ),
                None,
            ),
            (
                "funnel_no_strategy",
                (
                    "Понимаю, возможно, сейчас не время.\n\n"
                    "Могу предложить:\n"
                    "• изучить все возможности бота,\n"
                    "• подписаться на канал Виталия,\n"
                    "• перейти в библиотеку книг."
                ),
                None,
            ),
            (
                "funnel_objection_intro",
                (
                    "Давай бережно посмотрим, что именно тебя останавливает.\n\n"
                    "Напиши свободно: это может быть сомнение, страх, недоверие, вопрос про деньги, время или что-то ещё."
                ),
                None,
            ),
            (
                "funnel_landing_intro",
                (
                    "Отлично!\n\n"
                    "Сейчас я перенаправлю тебя на страницу записи.\n"
                    "Там ты:\n"
                    "1. заполнишь короткую анкету,\n"
                    "2. выберешь удобное время,\n"
                    "3. оплатишь 1500 руб.\n\n"
                    "После оплаты получишь подтверждение и ссылку на Zoom."
                ),
                None,
            ),
            (
                "funnel_resume_text",
                (
                    "С возвращением!\n\n"
                    "Что хочешь сделать?"
                ),
                None,
            ),
            (
                "funnel_fallback_text",
                "Не получилось безопасно разобрать ответ. Попробуй ещё раз или выбери другой раздел.",
                None,
            ),
            (
                "crisis_contacts",
                "Если тебе сейчас очень тяжело — пожалуйста, не оставайся с этим один. Позвони тем, кто может помочь прямо сейчас:\n\n"
                "📞 112 — экстренная помощь\n"
                "📞 8-800-100-49-94 — кризисная линия доверия для взрослых и детей\n"
                "📞 +7 495 989-50-50 — телефон психологической помощи МЧС России\n"
                "💬 @NaumovVitaliy — служба заботы, там ответит живой человек",
                None,
            ),
            (
                "session_completed",
                "Сессия завершена. Выбери следующий шаг.",
                None,
            ),
            (
                "btn_ai_finish_dialogue",
                "Завершить диалог",
                None,
            ),
            (
                "funnel_reminder_intro",
                "Ты заходил(а) в практику, но ещё не выбрал(а) медитацию. Можешь вернуться и продолжить в удобный момент.",
                None,
            ),
            (
                "funnel_reminder_listen",
                "Ты выбрал(а) практику, но ещё не завершил(а) этот этап. Можно вернуться к медитациям и пройти её в удобное время.",
                None,
            ),
            (
                "funnel_reminder_reflection",
                "Ты уже прослушал(а) гипносессию, но ещё не записал(а) свои инсайты. Вернись и опиши ощущения, чтобы получить персональный разбор.",
                None,
            ),
            (
                "funnel_reminder_strategy",
                "У тебя уже есть первый инсайт. Если хочешь углубиться, можно перейти к блоку про стратегическую сессию и посмотреть следующий шаг.",
                None,
            ),
            (
                "funnel_reminder_common",
                "Вот что часто происходит у тех, кто доходит до конца воронки: появляется больше ясности, понимание внутреннего конфликта и конкретный следующий шаг.",
                None,
            ),
            (
                "strategy_session_intro",
                (
                    "Это диалог про стратегическую сессию. "
                    "Я помогу быстро понять, с каким запросом тебе лучше на неё идти, и если нужно, подведу к записи."
                ),
                None,
            ),
            ("btn_funnel_landing", "📅 Записаться на стратегическую сессию", "https://psiholog-naumov.ru/str_sesiya_tt"),
            ("btn_vitaly_channel", "📢 Подписаться на канал", "https://t.me/lifeuspeh"),
            ("btn_vitaly_questions", "✉️ Задать свой вопрос Виталию", "https://t.me/NaumovVitaliy"),
            (
                "main_reminder_first",
                "Привет! Ты давно не заходил(а). Загляни — у нас есть практики, ИИ-диалоги и много полезного.",
                None,
            ),
            (
                "main_reminder_common",
                "Напоминаю, что в боте доступны медитативные практики, персональные ИИ-сессии и библиотека книг. Возвращайся!",
                None,
            ),
            ("journal_video", "Видеоурок по саморефлексии", None),
        ]

        for key, content_text, url in default_content:
            if not await session.get(Content, key):
                session.add(Content(key=key, text_content=content_text, button_text=None, link_url=url))

        template_check = await session.execute(select(Habit).where(Habit.user_id == None))
        if not template_check.scalars().first():
            templates = [
                Habit(name="🧘 Утренняя медитация (10 мин)", user_id=None),
                Habit(name="💧 Выпить стакан воды", user_id=None),
                Habit(name="📖 Чтение книги (20 мин)", user_id=None),
                Habit(name="🏃‍♂️ Прогулка / Спорт", user_id=None)
            ]
            session.add_all(templates)

        if not await session.get(FunnelConfig, 1):
            session.add(FunnelConfig(id=1))

        # Journal config and one-time seed
        j_config = await session.get(JournalConfig, 1)
        if not j_config:
            j_config = JournalConfig(id=1, points_per_reflection=10, questions_seeded=False)
            session.add(j_config)
            await session.flush()

        if not j_config.questions_seeded:
            q_count = await session.scalar(select(func.count(JournalQuestion.id)))
            if (q_count or 0) == 0:
                for idx, q_text in enumerate(CANONICAL_QUESTIONS, start=1):
                    session.add(JournalQuestion(
                        text=q_text,
                        order_num=idx,
                        is_active=True,
                    ))
            j_config.questions_seeded = True

        await session.commit()
        await run_data_migrations(session)


async def _seed_menu_configuration(session: AsyncSession) -> None:
    """Create the editable menu registry without changing existing content.

    The stable callback values are copied from the legacy keyboards. Existing
    rows are never overwritten, so an administrator's labels, order, enabled
    flags and actions survive restarts and upgrades.
    """
    sections = {
        "main": ("Главное меню", 0),
        "extended": ("Расширенное меню", 10),
        "thematic": ("Тематические ИИ-сессии", 20),
        "vitaly": ("Виталий Наумов", 30),
        "books": ("Библиотека книг", 40),
        "club": ("Закрытый клуб", 50),
        "referral": ("Реферальная программа", 60),
        "funnel_intro": ("Гипномедитации: вход", 70),
        "funnel_resume": ("Гипномедитации: возврат", 71),
        "funnel_reminder": ("Гипномедитации: напоминания", 72),
        "funnel_card": ("Гипномедитации: карточка практики", 73),
        "funnel_listening": ("Гипномедитации: прослушивание", 74),
        "funnel_analysis": ("Гипномедитации: навигация разбора", 75),
        "funnel_bridge": ("Гипномедитации: мостик", 76),
        "funnel_crisis": ("Гипномедитации: контакты помощи", 77),
        "funnel_strategy_offer": ("Стратегическая сессия: оффер", 78),
        "funnel_vitaly_method": ("Стратегическая сессия: Виталий", 79),
        "funnel_objection": ("Стратегическая сессия: сомнения", 80),
        "funnel_faq": ("Стратегическая сессия: вопросы", 81),
        "funnel_no_strategy": ("Стратегическая сессия: отказ", 82),
        "journal_entry": ("Дневник: вход", 90),
        "journal_menu": ("Дневник: меню", 91),
        "journal_question": ("Дневник: вопрос", 92),
        "habits_entry": ("Привычки: вход", 100),
        "subscription_main": ("Подписка: вход", 110),
        "subscription_info": ("Подписка: доступ", 111),
        "ai_completed": ("ИИ-сессии: завершение", 120),
    }
    for key, (title, order_num) in sections.items():
        if not await session.get(MenuSection, key):
            session.add(MenuSection(key=key, title=title, enabled=True, order_num=order_num))
    await session.flush()

    buttons = {
        "main": [
            ("🎧 Гипно-медитации", "callback", "menu_funnel_start", None),
            ("🎯 Стратегическая сессия с Виталием", "callback", "menu_strategy_session", None),
            ("💬 ИИ-сессия ТрансКоучинг", "callback", "mode_free_session", None),
            ("🧭 Диагностическая ИИ-сессия", "callback", "mode_diagnostics", None),
            ("🔍 Комплексная ИИ-диагностика", "callback", "mode_express", None),
            ("🗂 Тематические ИИ-сессии", "callback", "menu_thematic_sessions", None),
            ("📔 Дневник саморефлексии", "callback", "mode_journal", None),
            ("✅ Создать полезные привычки", "callback", "mode_habits", None),
            ("🔑 Подписка", "callback", "open_payment_menu", None),
            ("✨ Расширенное меню", "callback", "open_extended_menu", None),
        ],
        "extended": [
            ("🎓 Академия Транскоучинг", "web_app", None, "btn_academy"),
            ("Канал Виталия", "url", None, "btn_rich_thinking"),
            ("Истории трансформаций", "url", None, "btn_transfo"),
            ("🔑 Подписка", "callback", "open_payment_menu", None),
            ("📚 Библиотека книг", "callback", "menu_books", None),
            ("🔐 Закрытый клуб", "callback", "menu_closed_club", None),
            ("🤝 Реферальная программа", "callback", "menu_referral", None),
            ("👤 Виталий Наумов", "callback", "menu_vitaly", None),
            ("⬅️ Назад", "callback", "back_to_main_menu_text", None),
        ],
        "thematic": [
            ("⚡ Стресс и выгорание", "callback", "thematic_stress", None),
            ("💎 Самооценка и синдром самозванца", "callback", "thematic_self_esteem", None),
            ("👥 Отношения и границы", "callback", "thematic_relationships", None),
            ("🌙 Сон и восстановление", "callback", "thematic_sleep_recovery", None),
            ("🎯 Мотивация и цели", "callback", "thematic_motivation_goals", None),
            ("❤️ Эмоциональный интеллект", "callback", "thematic_emotional_intelligence", None),
            ("⬅️ В главное меню", "callback", "back_to_main_menu", None),
        ],
        "vitaly": [
            ("📅 Записаться на консультацию", "web_app", None, "btn_vitaly_consult"),
            ("🎤 Спикерство / Пригласить", "web_app", None, "btn_vitaly_speaker"),
            ("⬅️ Назад", "callback", "open_extended_menu", None),
        ],
        "club": [
            ("📚 Полезные материалы", "url", None, "btn_club_materials"),
            ("📰 Полезные статьи", "url", None, "btn_club_articles"),
            ("💬 Присоединиться к чату", "url", None, "btn_club_chat"),
            ("📅 Записаться на сессию", "web_app", None, "btn_club_session"),
            ("❓ Задать вопрос", "url", None, "btn_club_question"),
            ("🎭 Афиша мастер-классов", "url", None, "btn_club_masterclass"),
            ("⬅️ Назад", "callback", "open_extended_menu", None),
        ],
        "referral": [
            ("🔗 Создать ссылку-приглашение", "callback", "ref_create_link", None),
            ("👥 Посмотреть рефералов", "callback", "ref_view_list_0", None),
            ("🔍 Узнать своего реферера", "callback", "ref_who_invited", None),
            ("💰 Посмотреть баллы", "callback", "ref_balance", None),
            ("⬅️ Назад", "callback", "open_extended_menu", None),
        ],
        "funnel_intro": [
            ("Да, начнём", "callback", "funnel_intro_start", None),
            ("Узнать подробнее о методе", "callback", "funnel_about_method", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_resume": [
            ("🧘 Попробовать другую практику", "callback", "funnel_resume_choice", None),
            ("🎯 Узнать о стратегической сессии", "callback", "funnel_resume_sales", None),
            ("📚 Полезные материалы от Виталия", "callback", "funnel_resume_materials", None),
        ],
        "funnel_reminder": [
            ("Пройти практику", "callback", "menu_funnel_start", None),
            ("Узнать о стратегической сессии", "callback", "funnel_resume_strategy", None),
            ("Изучить все возможности бота", "callback", "funnel_to_main", None),
        ],
        "funnel_card": [
            ("Начать прослушивание", "callback", "funnel_listen_{meditation_id}", None),
            ("Вернуться к выбору", "callback", "funnel_choice_{page}", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_listening": [
            ("Я прослушал(а), готов(а) продолжить", "callback", "funnel_heard", None),
            ("Выбрать другую практику", "callback", "menu_funnel_start", None),
        ],
        "funnel_analysis": [
            ("Выбрать другую практику", "callback", "menu_funnel_start", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_bridge": [
            ("Разобраться глубже", "callback", "funnel_offer_requested", None),
            ("Побуду с этим", "callback", "funnel_no_strategy_selected", None),
        ],
        "funnel_crisis": [
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_strategy_offer": [
            ("Записаться на сессию", "callback", "funnel_open_landing", None),
            ("У меня вопросы", "callback", "funnel_show_faq", None),
            ("Хочу, но что-то останавливает", "callback", "funnel_start_objections", None),
            ("Подробнее о Виталии", "callback", "funnel_vitaly_method", None),
            ("Выбрать другую практику", "callback", "menu_funnel_start", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_vitaly_method": [
            ("🔎 Узнать подробнее", "callback", "funnel_show_sales", None),
            ("Пока нет, спасибо", "callback", "funnel_interest_no", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_objection": [
            ("🎯 Записаться на сессию", "callback", "funnel_open_landing", None),
            ("⬅️ Вернуться назад", "callback", "funnel_show_sales", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_faq": [
            ("Да, записываюсь на сессию", "callback", "funnel_open_landing", None),
            ("Задать свой вопрос Виталию", "url", None, "btn_vitaly_questions"),
            ("Хочу понять, что меня останавливает", "callback", "funnel_start_objections", None),
            ("⬅️ Вернуться назад", "callback", "funnel_show_sales", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "funnel_no_strategy": [
            ("📢 Подписаться на канал", "url", None, "btn_vitaly_channel"),
            ("Выбрать другую практику", "callback", "menu_funnel_start", None),
            ("Главное меню", "callback", "funnel_to_main", None),
        ],
        "journal_entry": [
            ("📊 Открыть дневник", "callback", "journal_open_stats", None),
            ("▶️ Начать рефлексию", "callback", "j_start", None),
            ("📝 Оперативные записи", "callback", "quick_notes", None),
            ("⬅️ Главное меню", "callback", "back_to_main_menu_text", None),
        ],
        "journal_menu": [
            ("▶️ Пройти рефлексию сегодня", "callback", "j_start", None),
            ("▶️ Продолжить", "callback", "j_res:{entry_id}", None),
            ("🔄 Начать заново", "callback", "j_rst:{entry_id}", None),
            ("👁 Посмотреть запись за сегодня", "callback", "j_h_v:{completed_id}", None),
            ("🎬 Видеоинструкция", "callback", "j_video", None),
            ("⏰ Время напоминания", "callback", "j_time_menu", None),
            ("📝 Оперативные записи", "callback", "quick_notes", None),
            ("📖 Мои прошлые записи", "callback", "j_h_p:0", None),
            ("📈 Моя динамика", "callback", "j_dyn_menu", None),
            ("🧘 Формирование привычек", "callback", "mode_habits", None),
            ("⬅️ Главное меню", "callback", "back_to_main_menu_text", None),
        ],
        "journal_question": [
            ("⏭ Пропустить", "callback", "j_skip:{entry_id}:{position}", None),
            ("❌ Выйти", "callback", "j_exit", None),
        ],
        "habits_entry": [
            ("📂 Выбрать из каталога", "callback", "habit_open_catalog", None),
            ("➕ Создать привычку", "callback", "habit_create_new", None),
            ("📋 Мои привычки", "callback", "habit_my_list", None),
            ("🌍 Часовой пояс", "callback", "habit_timezone", None),
            ("📔 Дневник саморефлексии", "callback", "mode_journal", None),
            ("⬅️ В главное меню", "callback", "back_to_main_menu_text", None),
        ],
        "subscription_main": [
            ("💰 Пополнить баланс", "callback", "pay_topup_balance", None),
            ("💳 Оформить доступ к программе", "callback", "show_subscription_info", None),
            ("⬅️ Назад", "callback", "open_extended_menu", None),
        ],
        "subscription_info": [
            ("💳 Оформить / Продлить доступ", "callback", "sub_select_plan", None),
            ("✅ Включить автопродление", "callback", "sub_toggle_renewal", None),
            ("❌ Отменить автопродление", "callback", "sub_toggle_renewal", None),
            ("🎁 Ввести промокод", "callback", "sub_enter_promo", None),
            ("🤝 Бонус за приглашение", "callback", "menu_referral", None),
            ("⬅️ Главное меню", "callback", "back_to_main_menu_text", None),
        ],
        "ai_completed": [
            ("📝 Выводы по сессии", "callback", "ai_gen_summary:{topic_slug}:{dialogue_id}", None),
            ("▶️ Продолжить диалог", "callback", "ai_continue_dialogue:{topic_slug}:{dialogue_id}", None),
            ("🔄 Сформировать заново", "callback", "ai_regen_summary:{topic_slug}:{dialogue_id}", None),
            ("🔄 Новый разговор", "callback", "ai_new_dialogue:{topic_slug}", None),
            ("📔 Дневник саморефлексии", "callback", "mode_journal", None),
            ("🎯 Записаться на сессию", "callback", "funnel_open_landing", None),
            ("🏠 Главное меню", "callback", "main_menu_action", None),
        ],
    }
    for section_key, entries in buttons.items():
        existing_count = await session.scalar(select(func.count(MenuButton.id)).where(MenuButton.section_key == section_key))
        if existing_count:
            continue
        for order_num, (label, action_type, action_value, content_key) in enumerate(entries):
            if content_key:
                content = await session.get(Content, content_key)
                if content and content.button_text:
                    label = content.button_text
            session.add(MenuButton(
                section_key=section_key,
                label=label,
                action_type=action_type,
                action_value=action_value,
                content_key=content_key,
                source_type="static",
                legacy_callback=action_value,
                label_override=False,
                enabled=True,
                order_num=order_num,
            ))
    await sync_book_menu_buttons(session, commit=False)


async def _migrate_investor_menu_defaults(session: AsyncSession) -> None:
    """Apply the v3 demo labels only to untouched legacy defaults."""
    desired_labels = {
        "menu_funnel_start": "🎧 Гипно-медитации",
        "menu_strategy_session": "🎯 Стратегическая сессия с Виталием",
        "mode_free_session": "💬 ИИ-сессия ТрансКоучинг",
        "mode_diagnostics": "🧭 Диагностическая ИИ-сессия",
        "mode_express": "🔍 Комплексная ИИ-диагностика",
        "menu_thematic_sessions": "🗂 Тематические ИИ-сессии",
        "mode_journal": "📔 Дневник саморефлексии",
        "mode_habits": "✅ Создать полезные привычки",
        "open_payment_menu": "🔑 Подписка",
        "open_extended_menu": "✨ Расширенное меню",
    }
    legacy_labels = {
        "🧘 Практика - гипносессия",
        "🎯 Стратегическая сессия",
        "🧠 AI Свободная сессия",
        "🩺 AI Диагностика + сессия",
        "⚡️ AI Экспресс диагностика",
        "🧠 Тематические сессии",
        "📔 Дневник саморефлексии",
        "🧘 Формировать привычки",
        "💳 Подписка",
        "✨ Расширенное меню",
    }
    buttons = (await session.scalars(select(MenuButton))).all()
    for button in buttons:
        desired = desired_labels.get(button.action_value)
        if not desired or button.label_override or button.label not in legacy_labels:
            continue
        button.label = desired

    content_labels = {
        "btn_rich_thinking": ("💎 Богатое мышление", "Канал Виталия"),
        "btn_transfo": ("🦋 Трансформации", "Истории трансформаций"),
    }
    for key, (legacy, desired) in content_labels.items():
        content = await session.get(Content, key)
        if content and (not content.button_text or content.button_text == legacy):
            content.button_text = desired
        rows = (await session.scalars(
            select(MenuButton).where(MenuButton.content_key == key)
        )).all()
        for row in rows:
            if not row.label_override and row.label == legacy:
                row.label = desired


_book_menu_sync_lock = asyncio.Lock()


async def sync_book_menu_buttons(session: AsyncSession, *, commit: bool = True) -> None:
    """Add menu rows for books without changing existing admin settings."""
    async with _book_menu_sync_lock:
        books = (await session.scalars(select(Book).order_by(Book.id.asc()))).all()
        book_ids = {book.id for book in books}
        rows = (await session.scalars(
            select(MenuButton).where(
                MenuButton.section_key == "books",
                MenuButton.source_type == "book",
            ).order_by(
                MenuButton.source_id.asc(),
                MenuButton.enabled.desc(),
                MenuButton.id.asc(),
            )
        )).all()
        changed = False
        existing = {}
        for row in rows:
            if row.source_id not in book_ids:
                if row.enabled:
                    row.enabled = False
                    changed = True
                continue
            existing.setdefault(row.source_id, row)

        for book in books:
            if book.id in existing:
                continue
            session.add(MenuButton(
                section_key="books",
                label=book.title,
                action_type="url",
                action_value=book.url,
                source_type="book",
                source_id=book.id,
                label_override=False,
                enabled=True,
                order_num=-book.id,
            ))
            changed = True

        if not changed:
            return
        try:
            await session.flush()
            if commit:
                await session.commit()
        except IntegrityError:
            if not commit:
                raise
            await session.rollback()


async def run_data_migrations(session: AsyncSession):
    import logging
    log = logging.getLogger("database")

    # 1. Promo codes normalization with collision detection
    promos = (await session.execute(select(PromoCode))).scalars().all()
    seen = {}
    collision = False
    for p in promos:
        norm = (p.code or "").strip().upper()
        if norm in seen and seen[norm] != p.id:
            collision = True
            log.critical(
                f"Promo code collision detected during migration: '{p.code}' collides with ID {seen[norm]}"
            )
            break
        seen[norm] = p.id

    if not collision:
        for p in promos:
            if p.code:
                norm = p.code.strip().upper()
                if p.code != norm:
                    p.code = norm

    # 2. HabitConfig letters & informal tone
    habit_conf = await session.get(HabitConfig, 1)
    if habit_conf:
        if "M" in (habit_conf.reward_sequence or "") or "O" in (habit_conf.reward_sequence or ""):
            habit_conf.reward_sequence = "М,О,Л,О,Д,Е,Ц"
        if "Вы" in (habit_conf.final_message or ""):
            habit_conf.final_message = "Поздравляем! Ты прошёл(ла) путь героя и закрепил(а) привычку! 🏆"

    # 3. Tone of voice migration (safely update ONLY known legacy defaults)
    diag = await session.get(Content, "diagnostics_questions")
    if diag and diag.text_content == "1. Как вы себя чувствуете?\n2. Что вас беспокоит?":
        diag.text_content = "1. Как ты себя чувствуешь?\n2. Что тебя беспокоит?"

    strat_offer = await session.get(Content, "funnel_strategy_offer")
    if strat_offer and strat_offer.text_content and "На ней вы глубже разберёте запрос" in strat_offer.text_content:
        strat_offer.text_content = (
            "Если откликнулся этот разбор, следующим шагом может стать стратегическая сессия с Виталием.\n\n"
            "На ней ты глубже разберёшь запрос, увидишь ограничения и определишь точку роста."
        )

    choice_intro = await session.get(Content, "funnel_choice_intro")
    if choice_intro and choice_intro.text_content and "Выберите практику" in choice_intro.text_content:
        choice_intro.text_content = choice_intro.text_content.replace("Выберите практику", "Выбери практику")

    ref_link = await session.get(Content, "referral_link_text")
    if ref_link and ref_link.text_content == "🎁 Получить бонус (Ваша ссылка)":
        ref_link.text_content = "🎁 Получить бонус (Твоя ссылка)"

    disc = await session.get(Content, "disclaimer_text")
    if disc and disc.text_content == "<i>Бот не заменяет консультацию специалиста. Если вы в кризисной ситуации, обратитесь к профессионалу.</i>":
        disc.text_content = "<i>Бот не заменяет консультацию специалиста. Если ты в кризисной ситуации, обратись к профессионалу.</i>"

    sub_info = await session.get(Content, "subscription_info")
    if sub_info and (not sub_info.text_content or sub_info.text_content in {
        "Текст 'Информация о подписке'", "",
        "У вас нет активного доступа ко всем функциям.\n\nОформите доступ, чтобы пользоваться всеми возможностями бота!"
    }):
        sub_info.text_content = (
            "У тебя нет активного доступа ко всем функциям.\n\n"
            "Оформи доступ, чтобы пользоваться всеми возможностями бота!"
        )

    # 4. Buttons default text and URLs
    btn_landing = await session.get(Content, "btn_funnel_landing")
    if btn_landing:
        if not btn_landing.button_text or btn_landing.button_text == "📅 Записаться на стратегическую сессию":
            btn_landing.button_text = "🔗 Перейти на страницу записи"
        if not btn_landing.link_url or btn_landing.link_url == "https://psiholog-naumov.ru/sesiya":
            btn_landing.link_url = "https://psiholog-naumov.ru/str_sesiya_tt"

    btn_questions = await session.get(Content, "btn_vitaly_questions")
    if btn_questions:
        if not btn_questions.button_text or btn_questions.button_text == "✉️ Задать свой вопрос Виталию":
            btn_questions.button_text = "✉️ Задать свой вопрос Виталию"
        if not btn_questions.link_url or btn_questions.link_url == "https://t.me/username":
            btn_questions.link_url = "https://t.me/NaumovVitaliy"

    btn_channel = await session.get(Content, "btn_vitaly_channel")
    if btn_channel:
        if not btn_channel.button_text or btn_channel.button_text == "📢 Подписаться на канал":
            btn_channel.button_text = "📢 Подписаться на канал"
        if not btn_channel.link_url or btn_channel.link_url in {"https://t.me/channel", "https://t.me/", ""}:
            btn_channel.link_url = "https://t.me/lifeuspeh"

    # 5. Seed new content keys if missing
    new_defaults = [
        ("welcome_msg", (
            "Привет! 👋 Я — персональный бот-помощник психолога Виталия Наумова.\n\n"
            "Я буду общаться на ты, чтобы наш контакт был ближе и проще.\n\n"
            "Здесь ты найдёшь медитативные практики, персональные сессии с ИИ, трекер привычек и библиотеку знаний."
        ), None),
        ("funnel_timeout_unstarted", (
            "Ты открывал(а) карточку практики, но так и не запустил(а) её. "
            "Если готов(а) продолжить — просто выбери удобное время и начни."
        ), None),
        ("funnel_timeout_uncompleted", (
            "Ты начал(а) слушать медитацию, но мы не зафиксировали твои инсайты. "
            "Как появится время — заверши практику и поделись впечатлениями!"
        ), None),
        ("funnel_reminder_step_1", (
            "Привет! Как твоё состояние после недавней практики? "
            "Удалось ли заметить, как инсайты проявляются в повседневных делах?"
        ), None),
        ("funnel_reminder_step_2", (
            "Часто после первой практики поднимаются привычные тупики и повторяющиеся сценарии. "
            "На стратегической сессии с Виталием мы разбираем именно такие корневые паттерны."
        ), None),
        ("funnel_reminder_step_3", (
            "В работе над собой есть зоны, которые сложно увидеть в одиночку — психика защищает старые привычки. "
            "Взгляд со стороны помогает совершить качественный прорыв."
        ), None),
        ("funnel_reminder_step_4", (
            "Если у тебя есть сомнения или вопросы перед записью на стратегическую сессию — "
            "я здесь, чтобы помочь разобраться, или ты всегда можешь написать Виталию лично."
        ), None),
        ("funnel_reminder_step_5", (
            "Я рядом и всегда готов помочь тебе на пути к изменениям. "
            "Двери на стратегическую сессию открыты, когда ты почувствуешь готовность."
        ), None),
        ("funnel_reminder_7d_remarketing", (
            "Привет! Прошла неделя с нашего разговора. Как твои дела и настрой? "
            "Если захочешь вернуться к практике или обсудить стратегическую сессию — напиши мне!"
        ), None),
        ("funnel_resume_text", (
            "С возвращением! 👋 Ты уже пробовал(а) нашу практику...\n\n"
            "Что ты хочешь сделать дальше?"
        ), None),
    ]

    for key, text_content, url in new_defaults:
        item = await session.get(Content, key)
        if not item:
            session.add(Content(key=key, text_content=text_content, button_text=None, link_url=url))

    # 6. TestConfig -> TestSet migration
    diag_set = (await session.execute(select(TestSet).where(TestSet.slug == 'diagnostics'))).scalar_one_or_none()
    if not diag_set:
        old_cfg = await session.get(TestConfig, 1)
        diag_set = TestSet(
            id=1,
            name="Первичная диагностика",
            slug="diagnostics",
            is_enabled=old_cfg.is_enabled if old_cfg else True,
            is_archived=False,
            show_progress=old_cfg.show_progress if old_cfg else True,
            formulas_enabled=old_cfg.formulas_enabled if old_cfg else False,
            formulas_json=old_cfg.formulas_json if old_cfg else None,
            separate_result_prompt_enabled=old_cfg.separate_result_prompt_enabled if old_cfg else False,
            result_system_prompt=old_cfg.result_system_prompt if old_cfg else None,
            interpretation_input_mode=old_cfg.interpretation_input_mode if old_cfg else 'all',
            interpretation_selected_variables=old_cfg.interpretation_selected_variables if old_cfg else None,
        )
        session.add(diag_set)
        await session.flush()

    if diag_set.expected_question_count is None:
        diag_set.expected_question_count = 7
    if diag_set.title == "Первичная диагностика":
        diag_set.title = "Диагностическая ИИ-сессия"

    profile_set = (await session.execute(
        select(TestSet).where(TestSet.slug == PSYCHOLOGICAL_PROFILE_SET_SLUG)
    )).scalar_one_or_none()
    if not profile_set:
        profile_set = TestSet(
            title="Психологический профиль",
            slug=PSYCHOLOGICAL_PROFILE_SET_SLUG,
            description="Конфигурируемый набор из 12 утверждённых вопросов заказчика.",
            is_enabled=True,
            is_archived=False,
            show_progress=True,
            expected_question_count=12,
        )
        session.add(profile_set)
        await session.flush()
    elif profile_set.expected_question_count is None:
        profile_set.expected_question_count = 12

    await session.execute(
        text("UPDATE test_questions SET test_set_id = :set_id WHERE test_set_id IS NULL"),
        {"set_id": diag_set.id}
    )
    await session.execute(
        text("UPDATE test_sessions SET test_set_id = :set_id WHERE test_set_id IS NULL"),
        {"set_id": diag_set.id}
    )

    connected_configs = (await session.execute(
        select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug.in_(("diagnostics", "express")))
    )).scalars().all()
    connected_set_ids = {diag_set.id, profile_set.id}
    connected_set_topics = {
        diag_set.id: {"diagnostics"},
        profile_set.id: {"express"},
    }
    connected_set_ids.update(
        cfg.test_set_id for cfg in connected_configs if cfg.test_set_id is not None
    )
    for cfg in connected_configs:
        if cfg.test_set_id is not None:
            connected_set_topics.setdefault(cfg.test_set_id, set()).add(cfg.topic_slug)

    all_bound_configs = (await session.execute(
        select(TopicDialogueConfig).where(TopicDialogueConfig.test_set_id.in_(connected_set_ids))
    )).scalars().all()
    non_diagnostic_bound_set_ids = {
        cfg.test_set_id
        for cfg in all_bound_configs
        if cfg.test_set_id is not None and cfg.topic_slug not in {"diagnostics", "express"}
    }

    connected_questions = (await session.execute(
        select(TestQuestion).where(TestQuestion.test_set_id.in_(connected_set_ids))
    )).scalars().all()
    for question in connected_questions:
        if (
            question.test_set_id not in non_diagnostic_bound_set_ids
            and is_esoteric_question(question)
        ):
            question.is_active = False

    active_questions_by_set = {}
    for question in connected_questions:
        if question.is_active:
            active_questions_by_set.setdefault(question.test_set_id, []).append(question)

    active_counts = {}
    for set_id, topics in connected_set_topics.items():
        active_questions = active_questions_by_set.get(set_id, [])
        for topic in topics:
            active_counts[(set_id, topic)] = sum(
                not is_esoteric_question(question) for question in active_questions
            )

    def scenario_ready(test_set, topic_slug: str) -> bool:
        return bool(
            test_set
            and test_set.is_enabled
            and not test_set.is_archived
            and is_test_set_ready(
                test_set,
                active_counts.get((test_set.id, topic_slug), 0),
                topic_slug=topic_slug,
            )
        )

    diag_ready = scenario_ready(diag_set, "diagnostics")
    profile_ready = scenario_ready(profile_set, "express")

    # 7. TopicDialogueConfig defaults
    all_topics = (await session.execute(select(Topic))).scalars().all()
    thematic_slugs = {
        "stress", "self_esteem", "relationships",
        "sleep_recovery", "motivation_goals", "emotional_intelligence"
    }
    for t in all_topics:
        existing_cfg = (await session.execute(
            select(TopicDialogueConfig).where(TopicDialogueConfig.topic_slug == t.slug)
        )).scalar_one_or_none()
        is_diagnostics = (t.slug == "diagnostics")
        is_express = (t.slug == "express")
        is_thematic = (t.slug in thematic_slugs)
        if not existing_cfg:
            session.add(TopicDialogueConfig(
                topic_slug=t.slug,
                welcome_text=None,
                welcome_media_type=None,
                welcome_media_id=None,
                reset_dialogue_on_entry=not is_thematic,
                start_dialogue_button_enabled=False,
                start_dialogue_button_text="Начать диалог",
                prerecorded_first_message=None,
                pre_dialogue_test_enabled=(diag_ready if is_diagnostics else (profile_ready if is_express else False)),
                test_set_id=diag_set.id if is_diagnostics else (profile_set.id if is_express else None),
                summary_generation_enabled=False if is_diagnostics else is_thematic,
                summary_prompt=None,
            ))
        elif is_diagnostics:
            existing_cfg.test_set_id = diag_set.id
            if not diag_ready:
                existing_cfg.pre_dialogue_test_enabled = False
            existing_cfg.summary_generation_enabled = False
        elif is_express:
            if not existing_cfg.test_set_id:
                existing_cfg.test_set_id = profile_set.id
                existing_cfg.pre_dialogue_test_enabled = profile_ready
            else:
                configured_set = await session.get(TestSet, existing_cfg.test_set_id)
                configured_ready = scenario_ready(configured_set, "express")
                if not configured_ready:
                    existing_cfg.pre_dialogue_test_enabled = False

    funnel_topic = await session.get(Topic, "funnel_demo")
    if funnel_topic and funnel_topic.system_prompt and "show_offer" not in funnel_topic.system_prompt:
        funnel_topic.system_prompt += (
            "\n\nВозвращай только JSON-объект без пояснений. Поля: "
            "mode (normal, clinical или crisis), has_experience (boolean), "
            "has_question (boolean), is_skeptic (boolean), is_empty (boolean), "
            "show_offer (boolean), reply (string), bridge (string или null)."
        )

    await _seed_menu_configuration(session)
    await _migrate_investor_menu_defaults(session)
    await session.commit()
