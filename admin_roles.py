from __future__ import annotations

import inspect
from enum import Enum
from functools import wraps
from typing import Any

from aiogram.types import CallbackQuery

from config import OWNER_IDS
import database
from database import User


class AdminRole(str, Enum):
    OWNER = "owner"
    FULL_ADMIN = "full_admin"
    CONTENT_EDITOR = "content_editor"
    SUPPORT = "support"


class AdminPermission(str, Enum):
    PANEL = "panel"
    ADMIN_MANAGEMENT = "admin_management"
    CLIENTS = "clients"
    CONTENT = "content"
    PAYMENTS = "payments"
    MAILING = "mailing"
    SYSTEM = "system"


ROLE_LABELS = {
    AdminRole.OWNER: "Владелец",
    AdminRole.FULL_ADMIN: "Полный администратор",
    AdminRole.CONTENT_EDITOR: "Редактор контента",
    AdminRole.SUPPORT: "Поддержка",
}

ALL_PERMISSIONS = frozenset(AdminPermission)
ROLE_PERMISSIONS = {
    AdminRole.OWNER: ALL_PERMISSIONS,
    AdminRole.FULL_ADMIN: ALL_PERMISSIONS,
    AdminRole.CONTENT_EDITOR: frozenset({AdminPermission.PANEL, AdminPermission.CONTENT}),
    AdminRole.SUPPORT: frozenset({AdminPermission.PANEL, AdminPermission.CLIENTS}),
}


def normalize_admin_role(value: AdminRole | str | None) -> AdminRole:
    if isinstance(value, AdminRole):
        return value
    try:
        return AdminRole(value or AdminRole.FULL_ADMIN.value)
    except ValueError:
        return AdminRole.FULL_ADMIN


def role_label(value: AdminRole | str | None) -> str:
    return ROLE_LABELS[normalize_admin_role(value)]


def role_has_permission(
    role: AdminRole | str | None,
    permission: AdminPermission | str,
) -> bool:
    if role is None:
        return False
    normalized_role = normalize_admin_role(role)
    normalized_permission = AdminPermission(permission)
    return normalized_permission in ROLE_PERMISSIONS[normalized_role]


async def get_admin_role(user_id: int | None) -> AdminRole | None:
    if not user_id:
        return None
    if user_id in OWNER_IDS:
        return AdminRole.OWNER

    async with database.async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_admin:
            return None
        return normalize_admin_role(getattr(user, "admin_role", None))


async def user_has_permission(
    user_id: int | None,
    permission: AdminPermission | str,
) -> bool:
    role = await get_admin_role(user_id)
    return role is not None and role_has_permission(role, permission)


async def _deny_event(
    event: Any,
    *,
    clear_state: Any = None,
    message: str = "⛔️ Доступ запрещён.",
) -> None:
    if clear_state is not None:
        try:
            await clear_state.clear()
        except Exception:
            pass

    if isinstance(event, CallbackQuery) or hasattr(event, "data"):
        try:
            await event.answer(message, show_alert=True)
        except Exception:
            pass
        return

    answer = getattr(event, "answer", None)
    if answer:
        try:
            await answer(message)
        except Exception:
            pass


async def require_permission(
    event: Any,
    permission: AdminPermission | str,
    *,
    state: Any = None,
    denial_message: str | None = None,
) -> bool:
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "id", None)
    role = await get_admin_role(user_id)
    if role is not None and role_has_permission(role, permission):
        return True
    message = denial_message or "⛔️ Доступ запрещён."
    await _deny_event(event, clear_state=state, message=message)
    return False


def permission_for_callback(callback_data: str | None) -> AdminPermission | None:
    data = str(callback_data or "")
    if not data:
        return None

    if data == "admin_panel":
        return AdminPermission.PANEL

    if data == "admin_funnel_settings":
        return AdminPermission.CONTENT
    if data.startswith((
        "ureset_sub_",
        "ureset_balance_",
        "ureset_landing_",
        "ureset_full_",
    )):
        return AdminPermission.PAYMENTS
    if data.startswith((
        "ureset_dialog_",
        "ureset_habits_",
    )):
        return AdminPermission.SYSTEM
    if data.startswith((
        "ureset_funnel_",
        "ureset_remind_",
        "ureset_name_",
    )):
        return AdminPermission.CLIENTS
    if data.startswith(("confirm_ureset_", "ureset_confirmed_")):
        kind = data.split("_", 2)[-1].split(":", 1)[0].rsplit("_", 1)[0]
        if kind in {"sub", "balance", "landing", "full"}:
            return AdminPermission.PAYMENTS
        if kind in {"dialog", "habits"}:
            return AdminPermission.SYSTEM
        return AdminPermission.CLIENTS
    if data.startswith((
        "admin_funnel_landings",
        "admin_flanding",
        "admin_flandings_export_",
    )):
        return AdminPermission.PAYMENTS
    if data.startswith((
        "admin_funnel_stats",
        "admin_user_funnel",
    )):
        return AdminPermission.CLIENTS
    if data.startswith((
        "admin_funnel_config",
        "admin_fcfg_",
        "admin_funnel_tools",
        "admin_ftool_",
    )):
        return AdminPermission.SYSTEM
    if data.startswith((
        "admin_funnel_content",
        "admin_fcont_",
        "del_fcont_media_",
        "confirmed_del_fcont_media_",
        "admin_funnel_meds_",
        "admin_fmed",
        "confirm_admin_fmeddel_",
        "confirmed_admin_fmeddel_",
        "confirm_del_fcont_media_",
        "confirm_ts_delete_",
        "confirmed_ts_delete_",
    )):
        return AdminPermission.CONTENT

    if (
        data == "admin_manage_admins"
        or data.startswith("admin_manage_admins_")
        or data.startswith("del_admin_")
        or data.startswith("confirm_del_admin_")
        or data.startswith("confirmed_del_admin_")
        or data in {"add_admin_start"}
        or data.startswith("admin_role_edit_")
        or data.startswith("admin_role_set_")
    ):
        return AdminPermission.ADMIN_MANAGEMENT

    if (
        data.startswith((
            "admin_clients",
            "admin_user_profile_",
            "admin_user_funnel_",
            "admin_user_resets",
            "admin_block_toggle_",
            "admin_crisis_clear_",
            "admin_history_",
            "view_client_",
            "admin_user_ai_logs_",
        ))
    ):
        return AdminPermission.CLIENTS

    if data.startswith((
        "admin_mailing",
        "mail_",
        "mailing_",
    )):
        return AdminPermission.MAILING

    if data.startswith("admin_ai_log_file_"):
        parts = data.split("_")
        if len(parts) > 5 and parts[5].isdigit() and int(parts[5]) > 0:
            return AdminPermission.CLIENTS
        return AdminPermission.SYSTEM
    if data.startswith("admin_ai_log_"):
        parts = data.split("_")
        if len(parts) > 5 and parts[5].isdigit() and int(parts[5]) > 0:
            return AdminPermission.CLIENTS
        return AdminPermission.SYSTEM
    if data.startswith("export_ai_logs_"):
        parts = data.split("_")
        if len(parts) > 3 and parts[3].isdigit() and int(parts[3]) > 0:
            return AdminPermission.CLIENTS
        return AdminPermission.SYSTEM

    if data.startswith((
        "admin_payment",
        "admin_pay",
        "admin_subscribers",
        "admin_subscriptions",
        "admin_subs_",
        "admin_plog_",
        "admin_manage_plans",
        "admin_add_plan",
        "admin_edit_plan",
        "admin_toggle_plan",
        "admin_delete_plan",
        "confirm_admin_delete_plan_",
        "confirmed_admin_delete_plan_",
        "confirmed_adm_del_plan_",
        "admin_promocodes",
        "admin_create_promo",
        "admin_edit_promo",
        "admin_assign_promo",
        "admin_toggle_promo",
        "admin_delete_promo",
        "confirm_delete_promo_",
        "confirmed_delete_promo_",
        "admin_prodamus",
        "admin_toggle_sub",
        "admin_set_welcome_bonus",
        "toggle_plan_",
        "toggle_promo_",
    )):
        return AdminPermission.PAYMENTS

    if data.startswith((
        "admin_content",
        "admin_cat_",
        "admin_manage_buttons",
        "admin_menu_",
        "admin_topics",
        "admin_topic_",
        "edit_topic_",
        "download_topic_",
        "set_prompt_",
        "admin_kb_",
        "upload_kb_",
        "delete_kb_",
        "del_kb_",
        "confirm_del_kb_",
        "confirmed_del_kb_",
        "edit_cont_",
        "download_prompt_",
        "toggle_topic_paid_",
        "admin_funnel_",
        "admin_fcont_",
        "admin_fmed",
        "del_fcont_media_",
        "admin_habit_",
        "adm_add_tpl_start",
        "adm_del_tpl_",
        "confirm_adm_del_tpl_",
        "confirmed_adm_del_tpl_",
        "set_habit_",
        "set_game_",
        "adm_books_pg_",
        "add_book_start",
        "del_book_",
        "admin_book:",
        "admin_book_",
        "confirm_del_book_",
        "confirmed_del_book_",
        "admin_journal",
        "j_adm_",
        "admin_test_system",
        "admin_test_",
        "admin_test_sets",
        "testset_",
        "edit_testset_",
        "ts_",
        "admin_books",
        "add_book_",
        "delete_book",
        "cfg_",
        "toggle_topic_",
        "attach_kb_",
        "toggle_kb_",
        "del_cont_media_",
        "confirm_del_cont_media_",
        "confirmed_del_cont_media_",
    )):
        return AdminPermission.CONTENT

    if data.startswith((
        "automation_",
        "admin_automation_",
        "admin_followup",
        "admin_fu_",
        "admin_reminders",
        "admin_webhook",
        "confirmed_admin_gen_webhook_secret",
        "admin_logs",
        "admin_ai_",
        "admin_logs",
        "admin_ai_logs",
        "admin_ai_log_",
        "export_ai_logs_",
        "ai_provider_",
        "set_mem_",
        "set_prov_",
        "set_ai_",
        "set_ds_",
        "set_kie_",
        "set_openrouter_",
        "set_model_",
        "or_mod_",
        "quick_model_",
        "save_kie_field_",
        "check_kie_credits",
        "admin_toggle_habits_paid",
    )):
        return AdminPermission.SYSTEM

    if data.startswith((
        "confirm_set_pay_mode_",
        "set_pay_mode_",
        "admin_referral",
        "admin_ref_",
        "add_prov_start",
        "toggle_prov_",
        "edit_sub_price",
        "edit_msg_cost",
        "set_prodamus_key_url",
        "set_prodamus_key_secret",
        "set_prodamus_key_paid_content",
        "set_prodamus_key_tg_token",
        "adm_add_plan_start",
        "adm_edit_plan_",
        "adm_toggle_plan_",
        "adm_del_plan_",
        "set_plan_upgrade_target_",
        "set_upgrade_plan_",
        "edit_plan_field_",
        "set_duration_unit_",
        "toggle_promo_active_",
        "delete_promo_",
        "edit_promo_field_",
        "promo_plan_toggle_",
        "promo_toggle_all_plans_",
    )):
        return AdminPermission.PAYMENTS

    if data == "admin_section_clients":
        return AdminPermission.CLIENTS
    if data in {"admin_section_logs", "admin_section_automation"}:
        return AdminPermission.SYSTEM
    if data == "admin_section_ai":
        return AdminPermission.CONTENT
    if data == "admin_section_content":
        return AdminPermission.CONTENT
    if data == "admin_section_payments":
        return AdminPermission.PAYMENTS
    if data == "admin_section_admins":
        return AdminPermission.ADMIN_MANAGEMENT

    if data.startswith("admin_"):
        return AdminPermission.SYSTEM
    return None


def permission_for_state(state_name: str | None) -> AdminPermission | None:
    state = str(state_name or "")
    if not state:
        return None

    if state.startswith(("FollowupAdminStates:", "AutomationAdminStates:")):
        return AdminPermission.SYSTEM
    if state.startswith("FunnelAdminStates:"):
        state_value = state.split(":", 1)[1]
        if state_value.startswith(("edit_content", "add_", "edit_meditation")):
            return AdminPermission.CONTENT
        return AdminPermission.SYSTEM
    if state.startswith("PaymentStates:"):
        state_value = state.split(":", 1)[1]
        if state_value in {"add_provider_name", "add_provider_token", "set_token_price"}:
            return AdminPermission.PAYMENTS
        return None
    if state.startswith(("TestSetAdminStates:", "TopicAdminStates:", "JournalAdminStates:", "TestAdminStates:")):
        return AdminPermission.CONTENT
    if not state.startswith("AdminStates:"):
        return None

    state_value = state.split(":", 1)[1]
    if state_value.startswith("mailing"):
        return AdminPermission.MAILING
    if state_value == "add_admin":
        return AdminPermission.ADMIN_MANAGEMENT
    if state_value == "search_client":
        return AdminPermission.CLIENTS
    if state_value.startswith(("plan_", "edit_plan", "edit_promo", "set_promo", "set_single_payment_key", "set_welcome_bonus")):
        return AdminPermission.PAYMENTS
    if state_value.startswith((
        "edit_content",
        "edit_url",
        "edit_book",
        "edit_topic_prompt",
        "upload_kb",
        "add_habit",
        "set_game",
        "add_book",
        "menu_button",
    )):
        return AdminPermission.CONTENT
    return AdminPermission.SYSTEM


def _state_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    candidate = kwargs.get("state")
    if candidate is not None and hasattr(candidate, "get_state"):
        return candidate
    for value in args:
        if hasattr(value, "get_state"):
            return value
    return None


async def _required_permission_for_event(event: Any, state: Any = None) -> AdminPermission | None:
    required = permission_for_callback(getattr(event, "data", None))
    if required is not None:
        return required

    text = getattr(event, "text", None)
    if isinstance(text, str):
        parts = text.split(maxsplit=1)
        command = parts[0] if parts else ""
        if command == "/admin":
            return AdminPermission.PANEL
        if command == "/help":
            return AdminPermission.SYSTEM

    if state is not None:
        return permission_for_state(await state.get_state())
    return None


def guard_admin_handler(callback):
    if getattr(callback, "__admin_access_guarded__", False):
        return callback

    @wraps(callback)
    async def guarded(event, *args, **kwargs):
        state = _state_from_call(args, kwargs)
        required = await _required_permission_for_event(event, state)
        if required is not None and not await require_permission(event, required, state=state):
            return None
        result = callback(event, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    guarded.__admin_access_guarded__ = True
    guarded.__admin_access_original__ = callback
    return guarded


def install_admin_route_guards(router: Any, module_globals: dict[str, Any] | None = None) -> None:
    wrapped_by_id: dict[int, Any] = {}
    replacements: list[tuple[str, Any, Any]] = []

    for observer in (getattr(router, "callback_query", None), getattr(router, "message", None)):
        if observer is None:
            continue
        for handler in observer.handlers:
            original = handler.callback
            key = id(original)
            guarded = wrapped_by_id.setdefault(key, guard_admin_handler(original))
            handler.callback = guarded
            if module_globals is not None:
                replacements.append((getattr(original, "__name__", ""), original, guarded))

    if module_globals is None:
        return
    for name, original, guarded in replacements:
        if name and module_globals.get(name) is original:
            module_globals[name] = guarded
