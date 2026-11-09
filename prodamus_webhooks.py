"""
Prodamus webhook handler.
Works with payment notifications and recurring subscription events.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select

from database import (
    PaymentIntent,
    ProdamusPayment,
    ReferralPaymentLog,
    SubscriptionConfig,
    SubscriptionPlan,
    TrialUsageHistory,
    User,
    UserSubscription,
    async_session_maker,
    get_all_admin_ids,
)
from time_utils import format_msk, msk_now

log = logging.getLogger("payments")
MSK = timezone(timedelta(hours=3))


def _format_user_ref(user: User | None, user_id: int) -> str:
    if not user:
        return f"[id=<code>{user_id}</code>]"
    user_ref = f"{user.first_name or ''}"
    if user.username:
        user_ref += f" (@{user.username})"
    user_ref += f" [id=<code>{user_id}</code>]"
    return user_ref


def _sort_dict(data):
    if isinstance(data, dict):
        return {k: _sort_dict(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return [_sort_dict(item) for item in data]
    return data


def _signature_payload(data: dict) -> str:
    json_str = json.dumps(_sort_dict(data), separators=(",", ":"), ensure_ascii=False)
    return json_str.replace("/", r"\/")


def create_prodamus_signature(data: dict, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        _signature_payload(data).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_prodamus_signature(data: dict, secret_key: str, received_signature: str) -> bool:
    expected = create_prodamus_signature(data, secret_key)
    return hmac.compare_digest(expected.lower(), received_signature.lower())


def _assign_nested(root: dict, key: str, value):
    if "[" not in key:
        root[key] = value
        return

    parts = []
    buf = []
    for ch in key:
        if ch == "[":
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        if ch == "]":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))

    current = root
    for part in parts[:-1]:
        if part == "":
            part = "0"
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _normalize_nested(value):
    if isinstance(value, dict):
        if value and all(str(k).isdigit() for k in value.keys()):
            return [_normalize_nested(value[k]) for k in sorted(value.keys(), key=lambda x: int(x))]
        return {k: _normalize_nested(v) for k, v in value.items()}
    return value


def _parse_customer_extra(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        extra = {}
        if ":" in raw_value and len(raw_value.split(":")) == 2 and raw_value.replace(":", "").isdigit():
            parts = raw_value.split(":")
            extra["user_id"] = parts[0].strip()
            extra["plan_id"] = parts[1].strip()
        for item in raw_value.split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                extra[key.strip()] = value.strip()
        return extra
    return {}


def _parse_order_context(order_id: str | None) -> dict:
    if not order_id:
        return {}
    raw = str(order_id).strip()
    parts = raw.split("_")
    if len(parts) < 4 or parts[0] != "prod":
        return {}
    plan_id = _to_int(parts[1])
    user_id = _to_int(parts[2])
    if not plan_id or not user_id:
        return {}
    return {
        "local_plan_id": plan_id,
        "target_plan_id": plan_id,
        "bot_user_id": user_id,
    }


def _parse_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    raw_value = str(raw_value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw_value, fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def _to_int(value) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_payment_key(
    order_id: str | None,
    subscription_id: int | None,
    event_code: str,
    payload: dict | None = None,
    subscription_data: dict | None = None,
) -> str | None:
    # 1. order_id / order_num
    if order_id:
        return str(order_id).strip()
    if payload:
        candidate_order = payload.get("order_id") or payload.get("order_num")
        if candidate_order:
            return str(candidate_order).strip()

    # 2. Real provider payment/transaction identifier from payload or subscription_data
    sources = []
    if payload:
        sources.append(payload)
    if subscription_data:
        sources.append(subscription_data)

    id_keys = [
        "payment_id", "transaction_id", "pay_id", "autopayment_id",
        "paymentId", "transactionId", "auto_payment_id", "provider_payment_id",
    ]
    for src in sources:
        for k in id_keys:
            val = src.get(k)
            if val not in (None, "", "None"):
                if subscription_id:
                    return f"sub-{subscription_id}-{val}"
                return f"pmt-{val}"

    # 3. Stable provider date/cycle/iteration
    cycle_keys = [
        "date_payment", "payment_date", "paid_at", "date", "created_at",
        "date_current_payment", "date_last_payment", "last_payment_date",
        "current_cycle", "cycle", "iteration", "payment_num", "period",
    ]
    for src in sources:
        for k in cycle_keys:
            val = src.get(k)
            if val not in (None, "", "None"):
                if subscription_id:
                    return f"sub-{subscription_id}-{event_code}-{val}"
                return f"pmt-{event_code}-{val}"

    # 4. Payload lacks any stable provider payment identifier: fail safely
    return None


async def _resolve_plan_and_user(session, payload: dict, customer_extra: dict, subscription_data: dict):
    order_context = _parse_order_context(payload.get("order_id") or payload.get("order_num"))
    combined_extra = {**order_context, **customer_extra}
    plan = None
    user_id = _to_int(combined_extra.get("bot_user_id") or combined_extra.get("user_id"))
    local_plan_id = _to_int(combined_extra.get("local_plan_id") or combined_extra.get("plan_id"))
    target_plan_id = _to_int(combined_extra.get("target_plan_id"))
    subscription_id = _to_int(subscription_data.get("id") or payload.get("subscription"))

    order_id = payload.get("order_id") or payload.get("order_num")
    if order_id and str(order_id).startswith("pi_"):
        token = str(order_id)[3:]
        intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
        if intent:
            if not user_id:
                user_id = intent.user_id
            if not local_plan_id:
                local_plan_id = intent.plan_id

    if target_plan_id:
        plan = await session.get(SubscriptionPlan, target_plan_id)
    if not plan and local_plan_id:
        plan = await session.get(SubscriptionPlan, local_plan_id)
    if not plan and subscription_id:
        plan = await session.scalar(
            select(SubscriptionPlan).where(SubscriptionPlan.prodamus_subscription_id == subscription_id)
        )

    user_sub = None
    if subscription_id:
        user_sub = await session.scalar(
            select(UserSubscription).where(UserSubscription.prodamus_subscription_id == subscription_id)
        )
        if user_sub and not user_id:
            user_id = user_sub.user_id
        if user_sub and not plan and user_sub.plan_id:
            plan = await session.get(SubscriptionPlan, user_sub.plan_id)

    return plan, user_id, user_sub, subscription_id


async def _handle_reminder(bot, session, plan: SubscriptionPlan | None, user_id: int | None, subscription_data: dict):
    if not plan or not user_id:
        return

    from crisis_safety import automatic_send_allowed
    if not await automatic_send_allowed(session, user_id, "prodamus_subscription_reminder"):
        return

    next_payment_at = _parse_datetime(
        subscription_data.get("date_next_payment")
        or subscription_data.get("next_payment_date")
    )
    amount = _to_float(subscription_data.get("sum") or subscription_data.get("amount")) or plan.price
    if next_payment_at:
        date_str = format_msk(next_payment_at)
    else:
        date_str = "в ближайшее время"

    try:
        await bot.send_message(
            user_id,
            f"Напоминаем: {date_str} продление доступа к образовательной программе "
            f"по тарифу «{plan.name}» на сумму {amount:.2f} руб."
        )
    except Exception:
        pass


async def _process_successful_payment(
        bot,
        session,
        payload: dict,
        config: SubscriptionConfig,
        plan: SubscriptionPlan,
        user_id: int,
        customer_extra: dict,
        subscription_data: dict,
        subscription_id: int | None,
):
    now = msk_now()
    order_id = payload.get("order_id") or payload.get("order_num")
    event_code = (
        subscription_data.get("action_code")
        or subscription_data.get("notification_code")
        or payload.get("payment_status")
        or "payment"
    )
    payment_key = _build_payment_key(
        order_id,
        subscription_id,
        event_code,
        payload=payload,
        subscription_data=subscription_data,
    )
    if not payment_key:
        log.critical(f"PRODAMUS_PAYMENT_KEY_ERROR: Payload lacks stable provider payment identity: {payload}")
        for admin_id in await get_all_admin_ids():
            try:
                await bot.send_message(
                    admin_id,
                    f"🚨 <b>Security Alert: Prodamus Missing Payment Identity</b>\n\n"
                    f"Вебхук не содержит стабильного идентификатора платежа (order_id, payment_id, date, cycle). "
                    f"Платёж не может быть надёжно дедуплицирован и отклонён от автоматической обработки.\n"
                    f"Payload: <code>{json.dumps(payload, ensure_ascii=False)[:300]}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    existing = await session.scalar(
        select(ProdamusPayment).where(
            ProdamusPayment.order_id == payment_key,
            ProdamusPayment.status == "completed",
        )
    )
    if existing:
        return

    amount = _to_float(payload.get("sum") or payload.get("payment_amount") or payload.get("products_total")) or plan.price

    # End-to-end integration with PaymentIntent for orders prefixed with 'pi_'
    if order_id and str(order_id).startswith("pi_"):
        token = str(order_id)[3:]
        from handlers import settle_payment_intent

        intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
        if intent:
            if existing or intent.status == "paid" or intent.paid_at:
                return

            # Wrong Provider enforcement:
            if intent.provider != "prodamus":
                log.critical(
                    f"WRONG_PROVIDER_ALERT: token={intent.token} "
                    f"intent_provider={intent.provider} user_id={intent.user_id} "
                    f"received_on=prodamus"
                )
                payment = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == payment_key))
                if payment:
                    payment.status = "wrong_provider_review"
                    payment.amount = amount or intent.final_amount
                    payment.plan_id = intent.plan_id
                    payment.user_id = intent.user_id
                    payment.prodamus_subscription_id = subscription_id
                    payment.event_type = event_code
                    payment.raw_payload = json.dumps(payload, ensure_ascii=False)
                    payment.processed_at = now
                else:
                    session.add(ProdamusPayment(
                        user_id=intent.user_id,
                        plan_id=intent.plan_id,
                        amount=amount or intent.final_amount,
                        order_id=payment_key,
                        prodamus_subscription_id=subscription_id,
                        event_type=event_code,
                        raw_payload=json.dumps(payload, ensure_ascii=False),
                        status="wrong_provider_review",
                        processed_at=now,
                    ))
                await session.commit()

                admin_alert_text = (
                    f"🚨 <b>Внимание: Оплата через Prodamus для заказа другого провайдера!</b>\n\n"
                    f"• Intent token: <code>{intent.token}</code>\n"
                    f"• Intent provider: <code>{intent.provider}</code>\n"
                    f"• Provider order id: <code>{payment_key}</code>\n"
                    f"• User id: <code>{intent.user_id}</code>\n"
                    f"• Expected amount: <b>{intent.final_amount:.2f} RUB</b>\n\n"
                    f"Платёж зафиксирован со статусом <code>wrong_provider_review</code>. "
                    f"Заказ назначен на <code>{intent.provider}</code>, поэтому автоматическое списание "
                    f"и продление доступа не производились. Требуется ручная проверка администратором."
                )
                for admin_id in await get_all_admin_ids():
                    try:
                        await bot.send_message(admin_id, admin_alert_text, parse_mode="HTML")
                    except Exception:
                        pass
                return

            raw_sum = _to_float(payload.get("sum") or payload.get("payment_amount") or payload.get("products_total"))
            if raw_sum is None or abs(round(raw_sum, 2) - round(intent.final_amount, 2)) > 0.01:
                log.error(
                    f"Prodamus: amount mismatch for intent {intent.token}: "
                    f"webhook amount={raw_sum}, intent.final_amount={intent.final_amount}"
                )
                for admin_id in await get_all_admin_ids():
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🚨 <b>Security Alert: Prodamus Amount Mismatch</b>\n\n"
                            f"• Intent token: <code>{intent.token}</code>\n"
                            f"• User ID: <code>{intent.user_id}</code>\n"
                            f"• Expected amount: <b>{intent.final_amount:.2f} RUB</b>\n"
                            f"• Webhook amount: <b>{raw_sum} RUB</b>\n\n"
                            f"Платёж отклонён без финансовых изменений. "
                            f"Провайдеру возвращён HTTP 200 для предотвращения повторов.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                return

            # Impossible / Late Provider State contract:
            # definitive failed/cancelled/expired -> later success:
            # do NOT silently accept money and return 200 while granting no access.
            if intent.status in ("cancelled", "expired", "failed"):
                received_status = payload.get("payment_status") or event_code
                log.critical(
                    f"LATE_SUCCESS_ALERT: token={intent.token} "
                    f"provider_order_id={payment_key} user_id={intent.user_id} "
                    f"expected_amount={intent.final_amount} received_status={received_status}"
                )
                payment = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == payment_key))
                if payment:
                    payment.status = "late_success_review"
                    payment.amount = amount or intent.final_amount
                    payment.plan_id = intent.plan_id
                    payment.user_id = intent.user_id
                    payment.prodamus_subscription_id = subscription_id
                    payment.event_type = event_code
                    payment.raw_payload = json.dumps(payload, ensure_ascii=False)
                    payment.processed_at = now
                else:
                    session.add(ProdamusPayment(
                        user_id=intent.user_id,
                        plan_id=intent.plan_id,
                        amount=amount or intent.final_amount,
                        order_id=payment_key,
                        prodamus_subscription_id=subscription_id,
                        event_type=event_code,
                        raw_payload=json.dumps(payload, ensure_ascii=False),
                        status="late_success_review",
                        processed_at=now,
                    ))
                await session.commit()

                admin_alert_text = (
                    f"🚨 <b>Внимание: Поздняя успешная оплата отменённого/истёкшего заказа!</b>\n\n"
                    f"• Intent token: <code>{intent.token}</code>\n"
                    f"• Provider order id: <code>{payment_key}</code>\n"
                    f"• User id: <code>{intent.user_id}</code>\n"
                    f"• Expected amount: <b>{intent.final_amount:.2f} RUB</b>\n"
                    f"• Received status: <b>{received_status}</b>\n\n"
                    f"Платёж зафиксирован со статусом <code>late_success_review</code>. "
                    f"Заказ находился в статусе <code>{intent.status}</code>, поэтому автоматическое списание "
                    f"и продление доступа не производились. Требуется ручная проверка администратором."
                )
                for admin_id in await get_all_admin_ids():
                    try:
                        await bot.send_message(admin_id, admin_alert_text, parse_mode="HTML")
                    except Exception:
                        pass

                try:
                    await bot.send_message(
                        intent.user_id,
                        f"⚠️ Мы получили оплату по твоему заказу, но его действие ранее было завершено ({intent.status}).\n"
                        f"Мы уже передали информацию администраторам для ручной проверки и активации доступа.",
                    )
                except Exception:
                    pass
                return

            payment = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == payment_key))
            if payment:
                payment.status = "completed"
                payment.amount = amount or intent.final_amount
                payment.plan_id = intent.plan_id
                payment.user_id = intent.user_id
                payment.prodamus_subscription_id = subscription_id
                payment.event_type = event_code
                payment.raw_payload = json.dumps(payload, ensure_ascii=False)
                payment.processed_at = now
            else:
                session.add(ProdamusPayment(
                    user_id=intent.user_id,
                    plan_id=intent.plan_id,
                    amount=amount or intent.final_amount,
                    order_id=payment_key,
                    prodamus_subscription_id=subscription_id,
                    event_type=event_code,
                    raw_payload=json.dumps(payload, ensure_ascii=False),
                    status="completed",
                    processed_at=now,
                ))

            provider_payment_id = str(payload.get("payment_id") or payload.get("id") or payment_key)
            profile_id = _to_int(subscription_data.get("profile") or payload.get("profile"))
            await settle_payment_intent(
                session,
                bot,
                intent,
                payment_charge_id=provider_payment_id,
                prodamus_subscription_id=subscription_id,
                prodamus_profile_id=profile_id,
            )
            return
    next_payment_at = _parse_datetime(
        subscription_data.get("date_next_payment")
        or subscription_data.get("next_payment_date")
    )
    profile_id = _to_int(subscription_data.get("profile") or payload.get("profile"))
    saved_discount = _to_int(customer_extra.get("discount_percent")) or 0

    payment = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == payment_key))
    if payment:
        payment.status = "completed"
        payment.amount = amount
        payment.plan_id = plan.id
        payment.user_id = user_id
        payment.prodamus_subscription_id = subscription_id
        payment.event_type = event_code
        payment.raw_payload = json.dumps(payload, ensure_ascii=False)
        payment.processed_at = now
    else:
        session.add(ProdamusPayment(
            user_id=user_id,
            plan_id=plan.id,
            amount=amount,
            order_id=payment_key,
            prodamus_subscription_id=subscription_id,
            event_type=event_code,
            raw_payload=json.dumps(payload, ensure_ascii=False),
            status="completed",
            processed_at=now,
        ))

    user_sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id).with_for_update()
    )

    if next_payment_at:
        end_date = next_payment_at
    else:
        base_end = user_sub.end_date if user_sub and user_sub.end_date > now else now
        if plan.duration_unit == "months":
            end_date = base_end + relativedelta(months=plan.duration_value)
        else:
            end_date = base_end + timedelta(days=plan.duration_value)

    start_date = user_sub.start_date if user_sub else now
    if user_sub:
        user_sub.plan_id = plan.id
        user_sub.start_date = start_date
        user_sub.end_date = end_date
        user_sub.auto_renewal = bool(subscription_id and plan.allow_auto_renewal)
        user_sub.payment_provider = "Prodamus"
        user_sub.payment_attempt_count = 0
        user_sub.last_payment_attempt = None
        user_sub.discount_percent = user_sub.discount_percent or saved_discount
        user_sub.prodamus_subscription_id = subscription_id
        user_sub.prodamus_profile_id = profile_id
    else:
        session.add(UserSubscription(
            user_id=user_id,
            plan_id=plan.id,
            start_date=start_date,
            end_date=end_date,
            auto_renewal=bool(subscription_id and plan.allow_auto_renewal),
            payment_provider="Prodamus",
            payment_attempt_count=0,
            discount_percent=saved_discount,
            prodamus_subscription_id=subscription_id,
            prodamus_profile_id=profile_id,
        ))

    if plan.is_trial:
        session.add(TrialUsageHistory(user_id=user_id, plan_id=plan.id, used_at=now))

    paying_user = await session.get(User, user_id)
    if paying_user:
        paying_user.subscription_end = end_date

    from handlers import grant_referral_payment_reward
    await grant_referral_payment_reward(
        session,
        bot,
        paying_user_id=user_id,
        amount=amount,
        source_payment_key=payment_key,
    )

    await session.commit()

    user_ref = _format_user_ref(paying_user, user_id)
    end_date_str = end_date.astimezone(MSK).strftime("%d.%m.%Y %H:%M")

    try:
        await bot.send_message(
            user_id,
            f"✅ Доступ к образовательной программе по тарифу «{plan.name}» успешно оформлен!\n"
            f"Действует до: {end_date_str} МСК."
        )
    except Exception as exc:
        log.error(f"Prodamus: не удалось уведомить пользователя {user_id}: {exc}")


    if config.notifications_enabled:
        for admin_id in await get_all_admin_ids():
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 Новый платеж (Prodamus)!\n\n"
                    f"Пользователь: {user_ref}\n"
                    f"Тариф программы: {plan.name}\n"
                    f"Сумма: {amount:.2f} руб.\n"
                    f"OrderId: {payment_key}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    log.info(f"ОПЛАТА | Prodamus | {user_ref} | {plan.name} | {amount:.2f} руб | order={payment_key}")


async def handle_prodamus_webhook(request: web.Request):
    bot = request.app["bot"]

    try:
        raw_json = await request.json()
        data = raw_json if isinstance(raw_json, dict) else {}
    except Exception:
        try:
            post_data = await request.post()
            data = {}
            for key, value in post_data.items():
                _assign_nested(data, key, value)
            data = _normalize_nested(data)
        except Exception:
            log.error("Prodamus webhook: не удалось прочитать тело запроса")
            return web.Response(status=400, text="Bad request")

    received_signature = data.pop("signature", "") or request.headers.get("Sign", "")
    if not received_signature:
        log.warning("Prodamus webhook: отсутствует подпись")
        return web.Response(status=400, text="Missing signature")

    async with async_session_maker() as session:
        config = await session.get(SubscriptionConfig, 1)
        if not config or not config.prodamus_secret_key:
            log.error("Prodamus webhook: секретный ключ не настроен")
            return web.Response(status=503, text="Not configured")

        if not verify_prodamus_signature(data, config.prodamus_secret_key, received_signature):
            log.warning("Prodamus webhook: неверная подпись")
            return web.Response(status=400, text="Invalid signature")

        customer_extra = _parse_customer_extra(data.get("customer_extra"))
        subscription_data = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
        plan, user_id, user_sub, subscription_id = await _resolve_plan_and_user(session, data, customer_extra, subscription_data)

        payment_status = str(data.get("payment_status", "")).lower()
        action_type = str(subscription_data.get("type", "")).lower()
        action_code = str(subscription_data.get("action_code", "")).lower()
        notification_code = str(subscription_data.get("notification_code", "")).lower()
        event_code = action_code or notification_code or payment_status or "unknown"

        if notification_code == "auto_payment_reminder":
            await _handle_reminder(bot, session, plan, user_id, subscription_data)
            return web.Response(status=200, text="OK")

        if action_type == "action" and action_code in {"deactivation", "finish"}:
            if user_sub:
                user_sub.auto_renewal = False
                if action_code == "finish" and user_sub.end_date < msk_now():
                    user_sub.end_date = msk_now()
                await session.commit()
            return web.Response(status=200, text="OK")

        success_markers = {"success", "paid", "completed"}
        is_success = payment_status in success_markers or action_code == "auto_payment"
        if not is_success:
            order_id = data.get("order_id") or data.get("order_num")
            if order_id and str(order_id).startswith("pi_"):
                token = str(order_id)[3:]
                intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
                if intent and intent.status == "pending":
                    intent.status = "cancelled"
                    await session.commit()

            payment_key = _build_payment_key(
                data.get("order_id") or data.get("order_num"),
                subscription_id,
                event_code,
                payload=data,
                subscription_data=subscription_data,
            )
            if payment_key:
                payment = await session.scalar(select(ProdamusPayment).where(ProdamusPayment.order_id == payment_key))
                if payment and payment.status != "completed":
                    payment.status = "failed"
                    payment.event_type = event_code
                    payment.raw_payload = json.dumps(data, ensure_ascii=False)
                    payment.processed_at = msk_now()
                    await session.commit()

            if config.notifications_enabled and user_id:
                user = await session.get(User, user_id)
                user_ref = _format_user_ref(user, user_id)
                for admin_id in await get_all_admin_ids():
                    try:
                        await bot.send_message(
                            admin_id,
                            f"⏹ Платёж прерван (Prodamus)\nПользователь: {user_ref}\n"
                            f"Тариф программы: {plan.name if plan else 'не определён'}\nOrderId: {payment_key}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            return web.Response(status=200, text="OK")

        if not plan or not user_id:
            log.warning(f"Prodamus webhook: не удалось сопоставить пользователя/тариф: {data}")
            return web.Response(status=200, text="OK")

        await _process_successful_payment(
            bot,
            session,
            data,
            config,
            plan,
            user_id,
            customer_extra,
            subscription_data,
            subscription_id,
        )

    return web.Response(status=200, text="OK")


def setup_prodamus_webhooks(app: web.Application, bot):
    app["bot"] = bot
    app.router.add_post("/webhooks/prodamus", handle_prodamus_webhook)


async def process_prodamus_payment(payload: dict, bot=None) -> web.Response:
    data = dict(payload)
    async with async_session_maker() as session:
        config = await session.get(SubscriptionConfig, 1)
        customer_extra = _parse_customer_extra(data.get("customer_extra"))
        subscription_data = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
        plan, user_id, user_sub, subscription_id = await _resolve_plan_and_user(session, data, customer_extra, subscription_data)

        payment_status = str(data.get("payment_status", "")).lower()
        action_code = str(subscription_data.get("action_code", "")).lower()
        success_markers = {"success", "paid", "completed"}
        is_success = payment_status in success_markers or action_code == "auto_payment"

        if not is_success:
            order_id = data.get("order_id") or data.get("order_num")
            if order_id and str(order_id).startswith("pi_"):
                token = str(order_id)[3:]
                intent = await session.scalar(select(PaymentIntent).where(PaymentIntent.token == token))
                if intent and intent.status == "pending":
                    intent.status = "cancelled"
                    await session.commit()
            return web.Response(status=200, text="Payment not successful")

        if not plan or not user_id:
            log.warning(f"process_prodamus_payment: could not resolve user/plan: {data}")
            return web.Response(status=400, text="Unresolved plan/user")

        await _process_successful_payment(
            bot,
            session,
            data,
            config,
            plan,
            user_id,
            customer_extra,
            subscription_data,
            subscription_id,
        )
        await session.commit()
    return web.Response(status=200, text="OK")
