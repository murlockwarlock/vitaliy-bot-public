#!/usr/bin/env python3
"""Validate and render the checked-in administrative navigation contract."""

from __future__ import annotations

import argparse
import ast
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NavigationRoute:
    section: str
    screen: str
    entry_callback: str
    back_callback: str
    handler_file: str
    handler_name: str
    behavioral_tests: tuple[str, ...]


NAVIGATION_ROUTES = (
    NavigationRoute(
        "👥 Клиенты",
        "Список клиентов",
        "admin_clients_list_0",
        "admin_clients_back:clientctx_0123456789abcdef",
        "handlers.py",
        "admin_clients_list",
        (
            "tests/test_admin_navigation_round2.py::test_root_entries_and_payment_settings_follow_one_step_back_route",
            "tests/test_admin_navigation_round2.py::test_client_context_keeps_parent_per_message",
            "tests/test_pr20_admin_behavioral.py::test_client_profile_callbacks_keep_two_open_list_contexts_separate",
        ),
    ),
    NavigationRoute(
        "👥 Клиенты",
        "Поиск и сброс",
        "admin_clients_start_search:clientctx_0123456789abcdef",
        "admin_clients_list_2",
        "handlers.py",
        "start_client_search",
        (
            "tests/test_pr20_admin_behavioral.py::test_client_search_reset_returns_to_the_source_page_through_real_handlers",
            "tests/test_pr20_admin_behavioral.py::test_client_search_cancel_keeps_page_three_after_real_handler_flow",
        ),
    ),
    NavigationRoute(
        "👥 Клиенты",
        "Профиль клиента",
        "admin_user_profile_1:clientctx_0123456789abcdef",
        "admin_clients_list_2",
        "handlers.py",
        "admin_user_profile",
        (
            "tests/test_pr20_admin_behavioral.py::test_client_profile_history_keeps_page_and_search_context_through_real_handlers",
            "tests/test_pr20_admin_behavioral.py::test_client_profile_callbacks_keep_two_open_list_contexts_separate",
        ),
    ),
    NavigationRoute(
        "👥 Клиенты",
        "История клиента",
        "admin_history_view_1_0:clientctx_0123456789abcdef",
        "admin_user_profile_1:clientctx_0123456789abcdef",
        "handlers.py",
        "admin_history_view",
        ("tests/test_admin_navigation_round2.py::test_client_profile_children_keep_the_message_context_token",),
    ),
    NavigationRoute(
        "👥 Клиенты",
        "Воронка клиента",
        "admin_user_funnel_1:clientctx_0123456789abcdef",
        "admin_clients_list_2:clientctx_0123456789abcdef",
        "funnel.py",
        "admin_user_funnel",
        ("tests/test_admin_navigation_round2.py::test_client_profile_children_keep_the_message_context_token",),
    ),
    NavigationRoute(
        "👥 Клиенты",
        "Сбросы клиента",
        "admin_user_resets_1:clientctx_0123456789abcdef",
        "admin_user_profile_1:clientctx_0123456789abcdef",
        "handlers.py",
        "admin_user_resets",
        ("tests/test_admin_navigation_round2.py::test_client_profile_children_keep_the_message_context_token",),
    ),
    NavigationRoute(
        "🤖 ИИ и знания",
        "Настройки ИИ",
        "admin_section_ai",
        "admin_panel",
        "handlers.py",
        "admin_section_menu",
        ("tests/test_admin_ux_contract.py::test_admin_routes_entry_and_back_to_parent_section",),
    ),
    NavigationRoute(
        "🤖 ИИ и знания",
        "AI-логи",
        "admin_ai_logs_0_all",
        "admin_ai_settings",
        "ai_logs_admin.py",
        "callback_admin_ai_logs",
        (
            "tests/test_v10_ai_logs_1to1.py::test_ai_logs_e2e_saving_and_admin_view_walkthrough",
            "tests/test_pr20_admin_behavioral.py::test_filtered_ai_logs_cannot_cross_user_or_reach_system_logs",
        ),
    ),
    NavigationRoute(
        "✏️ Контент и программы",
        "Контент",
        "admin_section_content",
        "admin_panel",
        "handlers.py",
        "admin_section_menu",
        ("tests/test_admin_ux_contract.py::test_admin_routes_entry_and_back_to_parent_section",),
    ),
    NavigationRoute(
        "✏️ Контент и программы",
        "Вопросы дневника",
        "admin_journal",
        "admin_section_content",
        "journal_admin_handlers.py",
        "admin_journal_menu_handler",
        ("tests/test_pr20_admin_behavioral.py::test_journal_question_actions_keep_the_current_page_context",),
    ),
    NavigationRoute(
        "✏️ Контент и программы",
        "Книги и тесты",
        "admin_test_sets_list",
        "admin_section_content",
        "test_set_admin_handlers.py",
        "list_test_sets",
        (
            "tests/test_pr20_admin_behavioral.py::test_standard_pagination_is_one_row_and_has_safe_boundaries",
            "tests/test_admin_ux_contract.py::test_admin_manage_buttons_workflow_and_back_routing",
        ),
    ),
    NavigationRoute(
        "⚙️ Автоматизации",
        "Автоматизации",
        "admin_section_automation",
        "admin_panel",
        "handlers.py",
        "admin_section_menu",
        ("tests/test_pr20_admin_behavioral.py::test_automation_and_followup_handlers_use_real_parent_route_and_pagination_row",),
    ),
    NavigationRoute(
        "⚙️ Автоматизации",
        "Редактор напоминаний",
        "admin_reminders_settings",
        "admin_section_automation",
        "handlers.py",
        "admin_reminders_settings",
        (
            "tests/test_pr20_admin_behavioral.py::test_reminder_editor_real_cycle_returns_to_automation_section",
            "tests/test_admin_navigation_audit.py::test_reminder_cancel_clears_state_and_late_text_cannot_save",
        ),
    ),
    NavigationRoute(
        "⚙️ Автоматизации",
        "Цепочки напоминаний",
        "admin_followup",
        "admin_section_automation",
        "followup_admin.py",
        "admin_followup_list",
        ("tests/test_pr20_admin_behavioral.py::test_automation_and_followup_handlers_use_real_parent_route_and_pagination_row",),
    ),
    NavigationRoute(
        "⚙️ Автоматизации",
        "Рассылки",
        "admin_mailing",
        "admin_section_automation",
        "handlers.py",
        "mailing_start",
        ("tests/test_admin_navigation_audit.py::test_mailing_navigation_leaves_input_mode",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Подписки и оплата",
        "admin_payment_settings",
        "admin_panel",
        "handlers.py",
        "admin_payment_settings",
        (
            "tests/test_admin_navigation_round2.py::test_root_entries_and_payment_settings_follow_one_step_back_route",
            "tests/test_admin_ux_contract.py::test_admin_routes_entry_and_back_to_parent_section",
            "tests/test_admin_navigation_round2.py::test_legacy_payment_menu_and_statistics_sources_are_normalized",
        ),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Статистика и доход",
        "admin_payment_stats:admin_payment_settings",
        "admin_payment_settings",
        "handlers.py",
        "admin_payment_stats",
        (
            "tests/test_admin_navigation_round2.py::test_payment_children_return_to_unified_payment_menu",
            "tests/test_pr20_admin_behavioral.py::test_payment_statistics_back_route_uses_each_real_entry_point",
        ),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Подписчики",
        "admin_subs_list_0_all",
        "admin_payment_settings",
        "handlers.py",
        "admin_subscribers",
        ("tests/test_admin_navigation_round2.py::test_payment_children_return_to_unified_payment_menu",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "История платежей",
        "admin_plog_0_all",
        "admin_payment_settings",
        "handlers.py",
        "admin_payment_log",
        ("tests/test_admin_navigation_round2.py::test_payment_children_return_to_unified_payment_menu",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Тарифы",
        "admin_manage_plans",
        "admin_payment_settings",
        "handlers.py",
        "admin_manage_plans",
        ("tests/test_admin_navigation_audit.py::test_admin_navigation_screens_clear_input_state",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Настройки оплаты",
        "admin_prodamus_settings",
        "admin_payment_settings",
        "handlers.py",
        "admin_prodamus_settings",
        ("tests/test_admin_navigation_round2.py::test_payment_settings_exposes_technical_actions_with_source_context",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Способы оплаты",
        "admin_pay_providers_0__admin_prodamus_settings",
        "admin_prodamus_settings",
        "handlers.py",
        "admin_pay_providers",
        ("tests/test_admin_navigation_round2.py::test_payment_editors_keep_the_immediate_parent_screen",),
    ),
    NavigationRoute(
        "💳 Подписки и оплата",
        "Стоимость сообщений",
        "admin_pay_prices__admin_prodamus_settings",
        "admin_prodamus_settings",
        "handlers.py",
        "admin_pay_prices",
        ("tests/test_admin_navigation_round2.py::test_payment_editors_keep_the_immediate_parent_screen",),
    ),
    NavigationRoute(
        "📋 Логи",
        "Общие логи",
        "admin_section_logs",
        "admin_panel",
        "handlers.py",
        "admin_section_menu",
        ("tests/test_admin_ux_contract.py::test_admin_routes_entry_and_back_to_parent_section",),
    ),
    NavigationRoute(
        "📋 Логи",
        "AI-логи из раздела логов",
        "admin_ai_logs_0_all_logs",
        "admin_section_logs",
        "ai_logs_admin.py",
        "callback_admin_ai_logs",
        ("tests/test_pr20_admin_behavioral.py::test_filtered_ai_logs_keep_client_role_and_file_context",),
    ),
    NavigationRoute(
        "👮 Администраторы",
        "Управление администраторами",
        "admin_manage_admins",
        "admin_panel",
        "handlers.py",
        "admin_manage_admins",
        (
            "tests/test_admin_navigation_round2.py::test_root_entries_and_payment_settings_follow_one_step_back_route",
            "tests/test_admin_ux_contract.py::test_admin_routes_entry_and_back_to_parent_section",
        ),
    ),
)


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _test_reference_exists(reference: str) -> bool:
    file_name, separator, test_name = reference.partition("::")
    return bool(
        separator
        and Path(file_name).is_file()
        and test_name in _test_functions(Path(file_name))
    )


def validate_routes() -> list[str]:
    errors: list[str] = []
    seen = set()
    for route in NAVIGATION_ROUTES:
        key = (route.section, route.screen)
        if key in seen:
            errors.append(f"duplicate route: {route.section} / {route.screen}")
        seen.add(key)
        handler_path = Path(route.handler_file)
        if not handler_path.is_file():
            errors.append(f"missing handler file: {route.handler_file}")
        elif route.handler_name not in _defined_functions(handler_path):
            errors.append(f"missing handler: {route.handler_file}:{route.handler_name}")
        if len(route.entry_callback.encode("utf-8")) > 64:
            errors.append(f"callback exceeds 64 bytes: {route.entry_callback}")
        if not route.back_callback:
            errors.append(f"missing back route: {route.section} / {route.screen}")
        for test_reference in route.behavioral_tests:
            if not _test_reference_exists(test_reference):
                errors.append(f"missing behavioral test: {test_reference}")
    return errors


def render_map(ci_status: dict[str, str]) -> str:
    errors = validate_routes()
    covered = sum(bool(route.behavioral_tests) for route in NAVIGATION_ROUTES)
    lines = [
        "# Admin navigation map",
        "",
        "Generated by `scripts/admin_navigation_map.py` in CI.",
        "",
        f"- Declared route screens: {len(NAVIGATION_ROUTES)}",
        f"- Screens with behavioral regression references: {covered}/{len(NAVIGATION_ROUTES)}",
        f"- Contract validation: {'PASS' if not errors else 'FAIL'}",
        f"- CI upstream: preflight={ci_status.get('preflight', 'unknown')}, "
        f"sqlite={ci_status.get('sqlite', 'unknown')}, postgres={ci_status.get('postgres', 'unknown')}",
        "",
        "| Раздел | Экран | Точка входа | Назад | Обработчик | Behavioral regression |\n"
        "|---|---|---|---|---|---|",
    ]
    for route in NAVIGATION_ROUTES:
        tests = "<br>".join(route.behavioral_tests)
        lines.append(
            f"| {route.section} | {route.screen} | `{route.entry_callback}` | "
            f"`{route.back_callback}` | `{route.handler_file}:{route.handler_name}` | {tests} |"
        )
    lines.extend([
        "",
        "## Проверяемые ограничения",
        "",
        "- Карта проверяет наличие заявленных обработчиков и behavioral-тестов, а CI исполняет эти тесты через реальные aiogram-обработчики.",
        "- Это не статический подсчёт callback и не утверждение, что каждая комбинация старых Telegram-сообщений и ролей является live E2E-проверкой.",
        "- Реальный Telegram Bot API, production database и внешние платёжные провайдеры в CI не используются.",
    ])
    if errors:
        lines.extend(["", "## Ошибки в карте", "", *[f"- {error}" for error in errors]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    ci_status = {
        "preflight": os.getenv("CI_PREFLIGHT_RESULT", "unknown"),
        "sqlite": os.getenv("CI_SQLITE_RESULT", "unknown"),
        "postgres": os.getenv("CI_POSTGRES_RESULT", "unknown"),
    }
    report = render_map(ci_status)
    Path(args.output).write_text(report, encoding="utf-8")
    errors = validate_routes()
    print(report, end="")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
