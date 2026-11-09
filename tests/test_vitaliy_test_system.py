import csv
import io
import json
import os
import sys
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import escape

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_universal_tests.db")
sys.modules.setdefault(
    "vector_store",
    SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None),
)

from database import (
    Base,
    TestAttempt,
    TestConfig,
    TestQuestion,
    TestSession,
    TestSet,
    User,
    async_session_maker,
    engine,
    init_db,
)
from ai_integration import StructuredAIResponse
from automation_events import wait_for_pending_automation_events
from file_parser import parse_formulas_file, parse_questions_file
from test_system_handlers import (
    UniversalTestStates,
    _finish_test,
    _save_answer,
    answer_test_option,
    answer_test_text,
    extract_test_start_directive,
)
from universal_tests import (
    calculate_formulas,
    json_dumps,
    make_text_answer_record,
    validate_test_definition,
)


HEADER = [
    "Номер вопроса", "Вопрос", "Комментарий", "Имя переменной", "Свой ответ", "Вывод кнопок",
    "Вариант ответа 1", "Вариант ответа 2", "Числовое значение 1", "Числовое значение 2",
]


def csv_bytes(rows, delimiter="|", encoding="utf-8"):
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode(encoding)


def xlsx_bytes(sheets):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        sheet_nodes = []
        rel_nodes = []
        overrides = []
        for sheet_index, (title, rows) in enumerate(sheets, 1):
            sheet_nodes.append(f'<sheet name="{escape(title)}" sheetId="{sheet_index}" r:id="rId{sheet_index}"/>')
            rel_nodes.append(
                f'<Relationship Id="rId{sheet_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_index}.xml"/>'
            )
            overrides.append(
                f'<Override PartName="/xl/worksheets/sheet{sheet_index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            xml_rows = []
            for row_index, row in enumerate(rows, 1):
                cells = []
                for column_index, value in enumerate(row):
                    if value is None or value == "":
                        continue
                    column = ""
                    number = column_index + 1
                    while number:
                        number, remainder = divmod(number - 1, 26)
                        column = chr(65 + remainder) + column
                    cells.append(f'<c r="{column}{row_index}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
                xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
            )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(sheet_nodes)}</sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{"".join(rel_nodes)}</Relationships>',
        )
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f'{"".join(overrides)}</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
    return stream.getvalue()


class FileFormatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_semicolon_comma_and_cp1251_files(self):
        row = [1, "Как вы себя чувствуете?", "Строка 1\nСтрока 2", "mood", "", 1, "Плохо", "Хорошо", 1, 3]
        for name, delimiter, encoding in [
            ("pipe.txt", "|", "utf-8"),
            ("excel.csv", ";", "utf-8"),
            ("comma.csv", ",", "utf-8"),
            ("windows.csv", ";", "cp1251"),
        ]:
            parsed = await parse_questions_file(io.BytesIO(csv_bytes([HEADER, row], delimiter, encoding)), name)
            self.assertEqual(parsed["questions"][0]["variable_name"], "mood")
            self.assertEqual(parsed["questions"][0]["comment"], "Строка 1\nСтрока 2")
            self.assertEqual(json.loads(parsed["questions"][0]["answer_options_json"])[1]["value"], 3.0)

    async def test_xlsx_questions_and_formulas(self):
        raw = xlsx_bytes([
            ("Вопросы", [HEADER, [1, "Энергия", "", "energy", "", 1, 1, 2, 1, 2]]),
            ("Формулы", [["Переменная", "Формула"], ["total", "energy * 2"]]),
        ])
        parsed = await parse_questions_file(io.BytesIO(raw), "test.xlsx")
        self.assertEqual(len(parsed["questions"]), 1)
        self.assertEqual(parsed["formulas"], [{"name": "total", "formula": "energy * 2"}])

    async def test_formula_only_csv_and_invalid_formula_source(self):
        formulas = await parse_formulas_file(
            io.BytesIO(csv_bytes([["Переменная", "Формула"], ["total", "score + 2"]], ";")),
            "formulas.csv",
        )
        strict = SimpleNamespace(
            text="Оценка", variable_name="score", allow_custom_answer=False, category="general",
            is_reverse=False, answer_options_json=json_dumps([{"text": "Да", "value": 1}]),
        )
        custom = SimpleNamespace(**{**strict.__dict__, "allow_custom_answer": True})
        self.assertEqual(validate_test_definition([strict], formulas), [])
        self.assertTrue(validate_test_definition([custom], formulas))


class TestDatabaseFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_maker() as session:
            session.add(User(id=101, first_name="Тестовый", name="Тестовый", is_admin=True, current_dialogue_id=7))
            session.add(TestConfig(id=1, formulas_json=json_dumps([{"name": "total_score", "formula": "score"}]), formulas_enabled=True))
            session.add(TestSet(
                id=1,
                name="Первичная диагностика",
                slug="diagnostics",
                is_enabled=True,
                show_progress=True,
                calculation_enabled=True,
                formulas_enabled=True,
                formulas_json=json_dumps([{"name": "total_score", "formula": "score"}]),
            ))
            session.add_all([
                TestQuestion(
                    text="Оценка", variable_name="score", sort_order=0, category="general", test_set_id=1,
                    answer_options_json=json_dumps([{"text": "Нет", "value": 0}, {"text": "Да", "value": 2}]),
                ),
                TestQuestion(text="Комментарий", variable_name="comment", sort_order=1, category="general", allow_custom_answer=True, test_set_id=1),
            ])
            session.add(TestSession(user_id=101, answers="[]", current_question_index=0, invocation_dialogue_id=7, test_set_id=1))
            await session.commit()

    async def test_init_db_seeds_test_config(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        async with async_session_maker() as session:
            self.assertIsNotNone(await session.get(TestConfig, 1))

    async def test_button_records_value_and_stale_button_is_rejected(self):
        next_index, total, answers = await _save_answer(101, "test_opt_0_1", is_text=False)
        self.assertEqual((next_index, total), (1, 2))
        self.assertEqual(answers[0]["answer"], "Да")
        self.assertEqual(answers[0]["numeric_value"], 2.0)
        with self.assertRaisesRegex(ValueError, "закрыт"):
            await _save_answer(101, "test_opt_0_0", is_text=False)

    async def test_strict_text_rejected_then_open_text_accepted(self):
        with self.assertRaisesRegex(ValueError, "выбери"):
            await _save_answer(101, "сам выберу", is_text=True)
        await _save_answer(101, "test_opt_0_0", is_text=False)
        next_index, total, answers = await _save_answer(101, "  мой ответ  ", is_text=True)
        self.assertEqual((next_index, total), (2, 2))
        self.assertEqual(answers[-1]["answer"], "мой ответ")

    async def test_real_callback_handler_acknowledges_and_advances(self):
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            data="test_opt_0_1",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        )
        state = AsyncMock()
        bot = AsyncMock()
        with patch("test_system_handlers._send_question", new=AsyncMock()) as send_question:
            await answer_test_option(callback, state, bot)
        callback.answer.assert_awaited()
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        send_question.assert_awaited_once_with(bot, 101, 1, state=state)

    async def test_real_text_handler_returns_validation_message(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), text="свой ответ", answer=AsyncMock())
        await answer_test_text(message, AsyncMock(), AsyncMock())
        self.assertIn("выбери", message.answer.await_args.args[0])

    async def test_completion_runs_separate_then_general_prompt_and_hides_technical_report(self):
        answers = [
            {"question_number": 1, "variable": "score", "question": "Оценка", "answer": "Да", "numeric_value": 2.0},
            {"question_number": 2, "variable": "comment", "question": "Комментарий", "answer": "Текст", "numeric_value": None},
        ]
        async with async_session_maker() as session:
            config = await session.get(TestConfig, 1)
            config.separate_result_prompt_enabled = True
            config.result_system_prompt = "Интерпретируй бережно"
            config.interpretation_input_mode = "formulas"
            ts = await session.get(TestSet, 1)
            if ts:
                ts.separate_interpretation_prompt = True
                ts.interpretation_prompt = "Интерпретируй бережно"
                ts.interpretation_input_mode = "formulas"
            await session.commit()

        loading = SimpleNamespace(delete=AsyncMock())
        bot = AsyncMock()
        bot.send_message.side_effect = [loading, SimpleNamespace()]
        state = AsyncMock()
        with (
            patch("test_system_handlers.get_standalone_ai_response", new=AsyncMock(return_value="Предварительный вывод")) as standalone,
            patch("test_system_handlers.get_ai_response_for_topic", new=AsyncMock(return_value="Понятный результат для клиента")) as general,
        ):
            await _finish_test(bot, 101, state, answers)

        self.assertIn("total_score: 2", standalone.await_args.args[1])
        self.assertIn("Предварительный вывод", general.await_args.args[1])
        visible = " ".join(str(call.args[1]) for call in bot.send_message.await_args_list)
        self.assertNotIn("total_score", visible)
        self.assertIn("Понятный результат", visible)
        state.set_state.assert_awaited_with("UserStates:chatting")

    async def test_pre_dialogue_crisis_data_sends_contacts_and_does_not_resume_dialogue(self):
        topic_slug = "pre-dialogue-crisis"
        async with async_session_maker() as session:
            attempt = TestAttempt(
                user_id=101,
                test_set_id=1,
                topic_slug=topic_slug,
                dialogue_id=7,
                purpose="pre_dialogue",
                status="in_progress",
                answers="[]",
            )
            session.add(attempt)
            await session.flush()
            test_session = await session.get(TestSession, 101)
            test_session.topic_slug = topic_slug
            test_session.invocation_topic_slug = topic_slug
            test_session.invocation_dialogue_id = 7
            test_session.attempt_id = attempt.id
            await session.commit()

        sent_messages = []

        async def send_message(chat_id, text, **kwargs):
            message = SimpleNamespace(
                message_id=len(sent_messages) + 1,
                chat=SimpleNamespace(id=chat_id),
                text=text,
                reply_markup=kwargs.get("reply_markup"),
                delete=AsyncMock(),
            )
            sent_messages.append(message)
            return message

        bot = AsyncMock()
        bot.send_message.side_effect = send_message
        state = AsyncMock()
        ai_config = SimpleNamespace(
            provider="OpenAI",
            context_limit_first=2,
            context_limit_recent=10,
            allow_fallback=False,
        )
        crisis_response = StructuredAIResponse(
            text='Я слышу тебя. <DATA>{"events":["crisis_detected"]}</DATA>',
            raw_payload={"test": True},
            http_status=None,
            finish_reason="stop",
        )

        with (
            patch(
                "ai_integration._prepare_clients",
                new=AsyncMock(return_value=(ai_config, None, object(), "test-model", None)),
            ),
            patch(
                "ai_integration._generate_chat_response",
                new=AsyncMock(return_value=crisis_response),
            ),
        ):
            await _finish_test(bot, 101, state, [])
        await wait_for_pending_automation_events()

        user_messages = [message for message in sent_messages if message.chat.id == 101]
        self.assertEqual(len(user_messages), 3)
        self.assertEqual(user_messages[1].text, "Я слышу тебя.")
        self.assertIsNone(user_messages[1].reply_markup)
        self.assertEqual(
            [
                button.text
                for row in user_messages[2].reply_markup.inline_keyboard
                for button in row
            ],
            ["Главное меню"],
        )
        self.assertNotIn("Режим активирован", " ".join(message.text for message in user_messages))
        state.set_state.assert_not_awaited()
        state.clear.assert_awaited_once()


class DirectiveAndKeyboardTests(unittest.TestCase):
    def test_directives_are_removed_without_leaking_to_client(self):
        for directive in ["[START_TEST]", "[run_test]", "[ЗАПУСТИТЬ_ТЕСТ]"]:
            should_start, clean = extract_test_start_directive(f"Начинаем. {directive}")
            self.assertTrue(should_start)
            self.assertEqual(clean, "Начинаем.")

    def test_horizontal_and_vertical_button_layout(self):
        import keyboards

        options = [SimpleNamespace(text=str(index)) for index in range(1, 4)]
        horizontal = keyboards.universal_test_answer_keyboard(options, True, 4)
        vertical = keyboards.universal_test_answer_keyboard(options, False, 4)
        self.assertEqual(len(horizontal.inline_keyboard), 1)
        self.assertEqual(len(vertical.inline_keyboard), 3)
        self.assertEqual(horizontal.inline_keyboard[0][1].callback_data, "test_opt_4_1")


if __name__ == "__main__":
    unittest.main()
