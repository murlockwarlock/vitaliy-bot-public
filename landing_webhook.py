import asyncio
import json

from aiohttp import web
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError

from config import OWNER_IDS
from database import FunnelLanding, LandingNotificationClaim, BotConfig, async_session_maker, get_all_admin_ids
from funnel import register_event
from logging_config import get_logger
from prodamus_webhooks import setup_prodamus_webhooks
from time_utils import msk_now

logger = get_logger("webhook")
payments_logger = get_logger("payments")

# Глобальная переменная для хранения текущего секрета (обновляется из БД)
_current_secret = ""


def _normalize_status(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"paid", "payed", "success", "succeeded", "completed"}:
        return "paid"
    if raw in {"form_filled", "anketa_submitted", "form_submitted", "anketa_filled"}:
        return "form_submitted"
    if raw in {"opened", "pending", "new", "payment_waiting", "in_work"}:
        return "opened"
    if raw in {"failed", "cancelled", "canceled", "false_order"}:
        return "failed"
    if raw in {"returned", "refunded", "refund"}:
        return "refunded"
    if raw in {"part_payed", "partial"}:
        return "partial"
    return "opened"


async def _handle_payment_update(request: web.Request):
    if not _current_secret:
        return web.json_response({"ok": False, "error": "webhook secret is not configured"}, status=503)

    secret = request.match_info["secret"]
    if secret != _current_secret:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    # GetCourse может слать form-urlencoded или JSON
    content_type = request.content_type or ""
    try:
        if "json" in content_type:
            payload = await request.json()
        else:
            form = await request.post()
            payload = dict(form)
    except Exception:
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)

    return web.json_response(await handle_incoming_webhook(payload, request.app["bot"]))


async def handle_incoming_webhook(payload: dict, bot) -> dict:
    token = str(
        payload.get("landing_token")
        or payload.get("token")
        or payload.get("deal_number")
        or payload.get("offer_code")
        or ""
    ).strip()

    status = _normalize_status(payload.get("status") or payload.get("deal_status") or payload.get("event"))
    logger.info("Landing webhook received: token=%s status=%s payload=%s", token, status, payload)

    user_id = payload.get("user_id")

    landing_id = None
    for _ in range(5):
        async with async_session_maker() as session:
            landing = None
            if token:
                landing = await session.scalar(select(FunnelLanding).where(FunnelLanding.token == token))
            elif user_id:
                landing = await session.scalar(select(FunnelLanding).where(FunnelLanding.user_id == int(user_id)).order_by(FunnelLanding.id.desc()))

            if not landing:
                uid = int(user_id) if user_id else 0
                landing = FunnelLanding(token=token or f"auto_{msk_now().timestamp()}", user_id=uid)
                session.add(landing)
                try:
                    await session.commit()
                    landing_id = landing.id
                    break
                except IntegrityError:
                    await session.rollback()
                    continue
            else:
                landing_id = landing.id
                break

    if not landing_id:
        return web.Response(status=500, text="Failed to resolve landing")

    async with async_session_maker() as session:
        landing = await session.get(FunnelLanding, landing_id)

        landing.status = status
        landing.external_payment_id = str(
            payload.get("payment_id") or payload.get("deal_id")
            or payload.get("deal_number") or payload.get("order_id")
            or payload.get("id") or ""
        ) or landing.external_payment_id

        # Имя: GetCourse шлёт user_name или first_name/last_name
        gc_name = payload.get("name") or payload.get("user_name")
        if not gc_name:
            first = payload.get("first_name") or ""
            last = payload.get("last_name") or ""
            gc_name = f"{first} {last}".strip() or None
        landing.customer_name = gc_name or landing.customer_name

        landing.customer_email = payload.get("email") or payload.get("user_email") or landing.customer_email
        landing.customer_phone = payload.get("phone") or payload.get("user_phone") or landing.customer_phone

        raw_amount = payload.get("amount") or payload.get("deal_cost")
        if raw_amount not in (None, ""):
            try:
                landing.amount = int(float(str(raw_amount)))
            except (ValueError, TypeError):
                pass

        landing.currency = payload.get("currency") or payload.get("deal_currency") or landing.currency
        landing.last_payload = json.dumps(payload, ensure_ascii=False)
        analytics_payload = json.dumps(
            {
                "landing_id": landing.id,
                "landing_token": landing.token,
                "status": status,
            },
            ensure_ascii=False,
        )

        should_notify_paid = False
        should_notify_form = False

        from funnel import deactivate_funnel

        claim_event = None
        if status == "paid":
            if not landing.paid_at:
                landing.paid_at = msk_now()
            if landing.user_id:
                await deactivate_funnel(session, landing.user_id, "paid", completed=True)
            if not landing.paid_notified:
                claim_event = "paid"

            await register_event(
                session,
                landing.user_id,
                "landing_payment",
                "landing_paid",
                meditation_id=landing.meditation_id,
                payload=analytics_payload,
            )
        elif status == "form_submitted":
            if landing.user_id:
                await deactivate_funnel(session, landing.user_id, "form_submitted", completed=True)
            if not landing.form_notified:
                claim_event = "form_submitted"

            await register_event(
                session,
                landing.user_id,
                "landing_payment",
                "landing_form_submitted",
                meditation_id=landing.meditation_id,
                payload=analytics_payload,
            )
        else:
            await register_event(
                session,
                landing.user_id,
                "landing_payment",
                f"landing_{status}",
                meditation_id=landing.meditation_id,
                payload=analytics_payload,
            )

        from database import User
        user = await session.get(User, landing.user_id) if landing.user_id else None
        username_str = f"@{user.username}" if (user and user.username) else "не указан"
        tg_id_str = str(landing.user_id) if landing.user_id else "не указан"
        user_name_str = landing.customer_name or (user.first_name if user else None) or "не указано"

        await session.commit()
        landing_id = landing.id
        landing_user_id = landing.user_id
        landing_amount = landing.amount
        landing_currency = landing.currency
        landing_customer_phone = landing.customer_phone
        landing_customer_email = landing.customer_email
        landing_token = landing.token
        landing_ext_payment_id = landing.external_payment_id

    admin_ids = await get_all_admin_ids()

    async def _deliver_to_recipient(
        recipient_type: str,
        recipient_id: int,
        send_coro_fn,
    ) -> bool:
        now = msk_now()
        claim_id = None
        async with async_session_maker() as session:
            claim = await session.scalar(
                select(LandingNotificationClaim).where(
                    LandingNotificationClaim.landing_id == landing_id,
                    LandingNotificationClaim.event_type == claim_event,
                    LandingNotificationClaim.recipient_type == recipient_type,
                    LandingNotificationClaim.recipient_id == recipient_id,
                )
            )
            if claim:
                if claim.status == "delivered":
                    return True
                # Check lease expiry (60 seconds)
                if claim.claimed_at and (now - claim.claimed_at).total_seconds() < 60:
                    for _ in range(4):
                        await asyncio.sleep(0.5)
                        async with async_session_maker() as check_session:
                            c = await check_session.get(LandingNotificationClaim, claim.id)
                            if c and c.status == "delivered":
                                return True
                    return False
                # Stale lease recovery: CAS takeover
                res = await session.execute(
                    update(LandingNotificationClaim)
                    .where(
                        LandingNotificationClaim.id == claim.id,
                        LandingNotificationClaim.claimed_at == claim.claimed_at,
                        LandingNotificationClaim.status != "delivered",
                    )
                    .values(claimed_at=now, status="claimed")
                )
                if res.rowcount == 0:
                    return False
                await session.commit()
                claim_id = claim.id
            else:
                new_claim = LandingNotificationClaim(
                    landing_id=landing_id,
                    event_type=claim_event,
                    recipient_type=recipient_type,
                    recipient_id=recipient_id,
                    status="claimed",
                    claimed_at=now,
                )
                session.add(new_claim)
                try:
                    await session.commit()
                    claim_id = new_claim.id
                except Exception:
                    await session.rollback()
                    return False

        # Attempt delivery
        delivery_ok = False
        try:
            await send_coro_fn()
            delivery_ok = True
        except Exception:
            logger.exception(
                "Failed delivery for landing_id=%s event=%s recipient_type=%s recipient_id=%s",
                landing_id, claim_event, recipient_type, recipient_id,
            )

        # Finalize or release claim
        async with async_session_maker() as session:
            if claim_id:
                c = await session.get(LandingNotificationClaim, claim_id)
                if c:
                    if delivery_ok:
                        c.status = "delivered"
                        c.delivered_at = msk_now()
                        await session.commit()
                        return True
                    else:
                        await session.delete(c)
                        await session.commit()
                        return False
            return delivery_ok

    if claim_event == "paid":
        recipients = []
        if landing_user_id:
            async def _send_customer():
                await bot.send_message(
                    landing_user_id,
                    "✅ Оплата за образовательную программу успешно получена! Спасибо.\n\nСкоро с тобой свяжутся или придёт подтверждение с деталями.",
                )
            recipients.append(("customer", landing_user_id, _send_customer))

        for admin_id in admin_ids:
            async def _send_admin(aid=admin_id):
                await bot.send_message(
                    aid,
                    f"💰 <b>Новая оплата с лендинга!</b>\n\n"
                    f"Событие: <code>paid</code>\n"
                    f"Сумма: {landing_amount or 0} {landing_currency or 'RUB'}\n"
                    f"Имя: {user_name_str}\n"
                    f"Телефон: {landing_customer_phone or 'не указан'}\n"
                    f"Email: {landing_customer_email or 'не указан'}\n"
                    f"@username: {username_str}\n"
                    f"Telegram ID: <code>{tg_id_str}</code>\n"
                    f"Токен лендинга: <code>{landing_token}</code>\n"
                    f"Payment ID: {landing_ext_payment_id or 'не указан'}",
                    parse_mode="HTML"
                )
            recipients.append(("admin", admin_id, _send_admin))

        for r_type, r_id, s_func in recipients:
            await _deliver_to_recipient(r_type, r_id, s_func)

        async with async_session_maker() as session:
            expected_recipients = len(recipients)
            delivered_count = await session.scalar(
                select(func.count(LandingNotificationClaim.id)).where(
                    LandingNotificationClaim.landing_id == landing_id,
                    LandingNotificationClaim.event_type == "paid",
                    LandingNotificationClaim.status == "delivered",
                )
            ) or 0
            if delivered_count >= expected_recipients:
                land = await session.get(FunnelLanding, landing_id)
                if land:
                    land.paid_notified = True
                    await session.commit()
                payments_logger.info(
                    "Landing payment success: user_id=%s token=%s payment_id=%s amount=%s currency=%s",
                    landing_user_id, landing_token, landing_ext_payment_id or "-", landing_amount, landing_currency
                )

    elif claim_event == "form_submitted":
        recipients = []
        if landing_user_id:
            async def _send_customer_form():
                await bot.send_message(
                    landing_user_id,
                    "📋 <b>Твоя анкета успешно принята!</b>\n\nСкоро мы свяжемся с тобой для уточнения деталей стратегической сессии.",
                    parse_mode="HTML"
                )
            recipients.append(("customer", landing_user_id, _send_customer_form))

        for admin_id in admin_ids:
            async def _send_admin_form(aid=admin_id):
                await bot.send_message(
                    aid,
                    f"📋 <b>Заполнена анкета на сессию!</b>\n\n"
                    f"Событие: <code>form_submitted</code>\n"
                    f"Имя: {user_name_str}\n"
                    f"Телефон: {landing_customer_phone or 'не указан'}\n"
                    f"Email: {landing_customer_email or 'не указан'}\n"
                    f"@username: {username_str}\n"
                    f"Telegram ID: <code>{tg_id_str}</code>\n"
                    f"Токен лендинга: <code>{landing_token}</code>",
                    parse_mode="HTML"
                )
            recipients.append(("admin", admin_id, _send_admin_form))

        for r_type, r_id, s_func in recipients:
            await _deliver_to_recipient(r_type, r_id, s_func)

        async with async_session_maker() as session:
            expected_recipients = len(recipients)
            delivered_count = await session.scalar(
                select(func.count(LandingNotificationClaim.id)).where(
                    LandingNotificationClaim.landing_id == landing_id,
                    LandingNotificationClaim.event_type == "form_submitted",
                    LandingNotificationClaim.status == "delivered",
                )
            ) or 0
            if delivered_count >= expected_recipients:
                land = await session.get(FunnelLanding, landing_id)
                if land:
                    land.form_notified = True
                    await session.commit()

    return {"ok": True, "status": status}


async def run_landing_webhook_server(bot):
    global _current_secret

    # Читаем настройки из БД
    async with async_session_maker() as session:
        conf = await session.get(BotConfig, 1)

    if not conf or not conf.webhook_enabled or not conf.webhook_secret:
        logger.info("Landing webhook is disabled (настройте через админку: Настройки Оплаты → Webhook лендинга)")
        return

    _current_secret = conf.webhook_secret
    port = conf.webhook_port or 8081

    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhook/landing-payment/{secret}", _handle_payment_update)
    setup_prodamus_webhooks(app, bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(
        "Landing webhook listening on http://0.0.0.0:%s/webhook/landing-payment/{secret}",
        port,
    )

    while True:
        # Периодически обновляем секрет из БД (на случай смены через админку)
        await asyncio.sleep(60)
        try:
            async with async_session_maker() as session:
                conf = await session.get(BotConfig, 1)
                if conf:
                    _current_secret = conf.webhook_secret or ""
        except Exception:
            pass
