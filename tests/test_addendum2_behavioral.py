from types import SimpleNamespace

from database import Book, MenuButton
from funnel import build_strategy_offer_keyboard, objection_response_keyboard
from keyboards import topic_finish_actions_keyboard
from md_utils import markdown_to_html, split_html_message
from universal_tests import calculate_formulas, expected_test_set_question_count, validate_test_definition


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_ai_markdown_is_rendered_as_telegram_html_and_numbered_lists_are_readable():
    rendered = markdown_to_html("## Заголовок\n\n**Текст**\n\n1. первый\n2) второй")

    assert "##" not in rendered
    assert "<b>Заголовок</b>" in rendered
    assert "<b>Текст</b>" in rendered
    assert "• первый" in rendered
    assert "• второй" in rendered


def test_long_ai_html_is_split_without_dangling_markup():
    chunks = split_html_message("<b>" + ("слово " * 1200) + "</b>", max_length=200)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert all(chunk.startswith("<b>") for chunk in chunks)
    assert all(chunk.endswith("</b>") for chunk in chunks)


def test_completed_session_always_offers_explicit_continuation():
    callbacks = {button.callback_data for button in _buttons(topic_finish_actions_keyboard("stress", 4))}

    assert "ai_continue_dialogue:stress:4" in callbacks
    assert "main_menu_action" in callbacks


def test_strategy_offer_hides_practice_switch_for_main_menu_entry():
    from_main = {button.callback_data for button in _buttons(build_strategy_offer_keyboard(False))}
    from_funnel = {button.callback_data for button in _buttons(build_strategy_offer_keyboard(True))}

    assert "menu_funnel_start" not in from_main
    assert "menu_funnel_start" in from_funnel


def test_objection_screen_has_back_and_main_menu_actions():
    callbacks = {button.callback_data for button in _buttons(objection_response_keyboard())}

    assert "funnel_show_sales" in callbacks
    assert "funnel_to_main" in callbacks


def test_new_persistent_fields_are_additive():
    assert Book.__table__.c.description.nullable is True
    assert MenuButton.__table__.c.topic_slug.nullable is True


def test_diagnostics_formula_validation_and_disabled_calculation_are_safe():
    question = SimpleNamespace(
        variable_name="answer_01",
        question_type="choice",
        answer_options_json='[{"text":"Да", "value": 1}]',
        allow_custom_answer=False,
        category=None,
        is_reverse=False,
    )

    assert validate_test_definition([question], [{"name": "score", "formula": "answer_01 * 2"}]) == []
    assert validate_test_definition([question], [{"name": "score", "formula": "missing * 2"}])
    assert calculate_formulas(
        [{"variable": "answer_01", "numeric_value": 3}],
        [{"name": "score", "formula": "answer_01 * 2"}],
    ) == {"score": 6.0}
    assert calculate_formulas([{"variable": "answer_01", "numeric_value": 3}], []) == {}
    assert expected_test_set_question_count(SimpleNamespace(slug="diagnostics")) == 7
