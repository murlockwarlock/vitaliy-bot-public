import csv
import io
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import funnel
from funnel import _funnel_conversion_text, render_funnel_stats_csv, summarize_funnel_journeys
from handlers import (
    classify_topic_admin_section,
    group_topics_for_admin,
    topic_admin_button_text,
)


def _topic(slug, name=None, *, prompt=None, topic_id=1):
    return SimpleNamespace(
        id=topic_id,
        slug=slug,
        name=name or slug,
        system_prompt=prompt,
        is_paid=True,
    )


def _event(event_id, user_id, event_type, created_at, meditation_id=1, payload=None):
    return SimpleNamespace(
        id=event_id,
        user_id=user_id,
        event_type=event_type,
        created_at=created_at,
        meditation_id=meditation_id,
        payload=payload,
    )


def _journey_events(user_id, start_id, start_time, *, complete=True):
    names = [
        "funnel_started",
        "choice_opened",
        "meditation_started",
        "reflection_submitted",
        "analysis_sent",
        "bridge_shown",
        "offer_requested",
        "signup_clicked",
        "landing_open_requested",
    ]
    if complete:
        names.extend(["landing_form_submitted", "landing_paid"])
    return [
        _event(start_id + index, user_id, event_type, start_time + timedelta(minutes=index))
        for index, event_type in enumerate(names)
    ]


def test_topic_admin_groups_preserve_known_sections_and_mark_non_ai_topics():
    topics = [
        _topic("habits", "Привычки", topic_id=11),
        _topic("strategy_session", "Стратегическая сессия", topic_id=10),
        _topic("funnel_demo", "Разбор", topic_id=9),
        _topic("free_session", "Свободная сессия", topic_id=8),
        _topic("journal", "Дневник", topic_id=7),
        _topic("custom_ai", "Своя тема", prompt="Ты помощник", topic_id=6),
        _topic("legacy_without_prompt", "Служебная тема", topic_id=5),
    ]

    grouped = group_topics_for_admin(topics)

    assert [topic.slug for topic in grouped["funnel"]] == ["funnel_demo", "strategy_session"]
    assert [topic.slug for topic in grouped["ai_sessions"]] == ["free_session"]
    assert [topic.slug for topic in grouped["thematic"]] == ["custom_ai"]
    assert [topic.slug for topic in grouped["journal"]] == ["journal"]
    assert [topic.slug for topic in grouped["non_ai"]] == ["habits", "legacy_without_prompt"]
    assert classify_topic_admin_section(_topic("habits")) == "non_ai"
    assert "без ИИ-диалога" in topic_admin_button_text(_topic("habits"), "non_ai")


def test_funnel_journeys_are_sequential_repeat_safe_and_period_scoped():
    base = datetime(2026, 1, 1, 12, 0, 0)
    events = _journey_events(1, 1, base)
    events.insert(6, _event(100, 1, "bridge_shown", base + timedelta(minutes=5, seconds=30)))
    events.insert(7, _event(101, 1, "signup_clicked", base + timedelta(minutes=7, seconds=30)))
    events.extend(_journey_events(1, 200, base + timedelta(days=2), complete=True))
    events.extend(_journey_events(2, 300, base + timedelta(days=2), complete=False))
    events.insert(0, _event(400, 3, "signup_clicked", base + timedelta(days=2, minutes=1)))

    stats = summarize_funnel_journeys(events)

    assert stats["starts"] == 3
    assert stats["selected"] == 3
    assert stats["bridge_shown"] == 3
    assert stats["signup_clicked"] == 3
    assert stats["forms"] == 2
    assert stats["paid"] == 2
    assert stats["conversion_denominators"]["signup_clicked"] == 3
    for key, denominator in stats["conversion_denominators"].items():
        if denominator:
            assert stats[key] <= denominator

    period_stats = summarize_funnel_journeys(
        events,
        since=base + timedelta(days=1),
    )
    assert period_stats["starts"] == 2
    assert period_stats["paid"] == 1


def test_direct_entries_reconstruct_old_history_without_duplicate_pages_or_returns():
    base = datetime(2026, 2, 1, 12, 0, 0)
    events = [
        _event(1, 10, "choice_opened", base),
        _event(2, 10, "choice_opened", base + timedelta(minutes=1)),
        _event(3, 10, "meditation_started", base + timedelta(minutes=2)),
        _event(4, 10, "choice_opened", base + timedelta(minutes=3)),
        _event(5, 10, "choice_opened", base + timedelta(minutes=4)),
        _event(6, 10, "return_to_main", base + timedelta(minutes=5)),
        _event(7, 10, "choice_opened", base + timedelta(minutes=6)),
    ]

    stats = summarize_funnel_journeys(events)

    assert stats["starts"] == 3
    assert stats["choices"] == 3
    assert stats["selected"] == 1
    assert len(stats["journeys"]) == 3


@pytest.mark.asyncio
async def test_menu_funnel_start_marks_one_explicit_new_journey():
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=12),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("funnel.show_meditation_choices", new=AsyncMock()) as show_choices:
        await funnel.menu_funnel_start(callback, state)

    show_choices.assert_awaited_once_with(callback, start_new_journey=True)


def test_late_payment_after_menu_exit_stays_with_original_landing_journey():
    base = datetime(2026, 2, 3, 12, 0, 0)
    events = _journey_events(13, 1, base, complete=False)
    events[8].payload = '{"landing_token":"late-payment-token"}'
    events.extend([
        _event(20, 13, "return_to_main", base + timedelta(minutes=9)),
        _event(21, 13, "funnel_started", base + timedelta(minutes=10)),
        _event(22, 13, "choice_opened", base + timedelta(minutes=11)),
        _event(
            23,
            13,
            "landing_paid",
            base + timedelta(minutes=12),
            payload='{"landing_id":77,"landing_token":"late-payment-token"}',
        ),
    ])

    stats = summarize_funnel_journeys(events)

    assert stats["starts"] == 2
    assert stats["landing"] == 1
    assert stats["paid"] == 1


def test_legacy_paid_status_is_reconciled_with_new_token_status():
    base = datetime(2026, 2, 5, 12, 0, 0)
    events = _journey_events(15, 1, base, complete=False)
    events[8].payload = '{"landing_token":"historic-status-token"}'
    events.extend([
        _event(20, 15, "landing_marked_paid", base + timedelta(minutes=10)),
        _event(
            21,
            15,
            "landing_marked_failed",
            base + timedelta(minutes=11),
            payload='{"landing_token":"historic-status-token"}',
        ),
    ])

    assert summarize_funnel_journeys(events)["paid"] == 0

    events.append(
        _event(
            22,
            15,
            "landing_paid",
            base + timedelta(minutes=12),
            payload='{"landing_token":"historic-status-token"}',
        )
    )
    assert summarize_funnel_journeys(events)["paid"] == 1


def test_landing_status_corrections_keep_only_final_payment_once_in_ui_and_csv():
    base = datetime(2026, 2, 4, 12, 0, 0)
    events = _journey_events(14, 1, base, complete=False)
    events[8].payload = '{"landing_token":"status-token"}'
    status_events = [
        (20, "landing_marked_paid", 10),
        (21, "landing_marked_failed", 11),
        (22, "landing_marked_paid", 12),
        (23, "landing_paid", 13),
        (24, "landing_paid", 14),
    ]
    events.extend(
        _event(
            event_id,
            14,
            event_type,
            base + timedelta(minutes=minute),
            payload='{"landing_id":88,"landing_token":"status-token"}',
        )
        for event_id, event_type, minute in status_events
    )

    stats = summarize_funnel_journeys(events)
    stats["period_label"] = "За всё время"

    assert stats["paid"] == 1
    assert _funnel_conversion_text(stats, "paid") == "1/1 (100.0%)"
    rows = list(csv.reader(io.StringIO(render_funnel_stats_csv(stats, "all"))))
    metric_rows = {row[0]: row for row in rows[2:] if row and row[0]}
    assert metric_rows["paid"][1:4] == ["1", "1", "100.0%"]


def test_webhook_and_manual_landing_statuses_share_one_stage_and_export_once():
    base = datetime(2026, 2, 2, 12, 0, 0)
    events = _journey_events(11, 1, base)
    events.extend([
        _event(100, 11, "landing_form_submitted", base + timedelta(minutes=11)),
        _event(101, 11, "landing_marked_form_submitted", base + timedelta(minutes=12)),
        _event(102, 11, "landing_paid", base + timedelta(minutes=13)),
        _event(103, 11, "landing_marked_paid", base + timedelta(minutes=14)),
    ])

    stats = summarize_funnel_journeys(events)
    stats["period_label"] = "За всё время"

    assert stats["forms"] == 1
    assert stats["paid"] == 1
    assert _funnel_conversion_text(stats, "forms") == "1/1 (100.0%)"
    assert _funnel_conversion_text(stats, "paid") == "1/1 (100.0%)"

    rows = list(csv.reader(io.StringIO(render_funnel_stats_csv(stats, "all"))))
    metric_rows = {row[0]: row for row in rows[2:] if row and row[0]}
    assert metric_rows["forms"][1:4] == ["1", "1", "100.0%"]
    assert metric_rows["paid"][1:4] == ["1", "1", "100.0%"]


def test_funnel_stats_csv_exposes_new_metrics_and_denominators():
    stats = {
        "period_label": "7 дней",
        "starts": 4,
        "choices": 4,
        "selected": 3,
        "reflections": 2,
        "analyses": 2,
        "strategy": 1,
        "bridge_shown": 2,
        "offer_requested": 1,
        "signup_clicked": 1,
        "landing": 1,
        "forms": 1,
        "paid": 1,
        "followups": 1,
        "resumes_menu": 0,
        "resumes_direct": 0,
        "conversion_denominators": {
            "choices": 4,
            "selected": 4,
            "reflections": 3,
            "analyses": 2,
            "bridge_shown": 2,
            "offer_requested": 2,
            "signup_clicked": 1,
            "landing": 1,
            "forms": 1,
            "paid": 1,
        },
        "per_meditation": [(1, {
            "started": 3,
            "reflections": 2,
            "analyses": 2,
            "bridge_shown": 2,
            "offer_requested": 1,
            "signup_clicked": 1,
            "landing": 1,
            "forms": 1,
            "paid": 1,
            "strategy": 1,
            "followups": 1,
        })],
    }

    rows = list(csv.reader(io.StringIO(render_funnel_stats_csv(stats, "7"))))
    metric_header = rows[1]
    metric_rows = {row[0]: row for row in rows[2:] if row and row[0]}

    assert metric_header == ["metric", "value", "denominator", "conversion"]
    for key in ("bridge_shown", "offer_requested", "signup_clicked"):
        assert key in metric_rows
        assert metric_rows[key][2]
        assert metric_rows[key][3].endswith("%")
    assert rows[-1][0:4] == ["1", "3", "2", "2"]
