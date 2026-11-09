from __future__ import annotations

import ast
import html
import json
import re
from dataclasses import dataclass
from typing import Any


TRUTHY_VALUES = {"1", "true", "+", "yes", "y", "да", "д", "истина"}
HORIZONTAL_VALUES = {"1", "row", "horizontal", "горизонтально", "в ряд", "ряд"}
LEGACY_CATEGORIES = {"body", "face", "age", "health", "abilities", "relations", "success"}
QUESTION_TYPE_TEXT = "text"
QUESTION_TYPE_SCALE = "scale"
QUESTION_TYPE_CHOICE = "choice"
QUESTION_TYPES = {QUESTION_TYPE_TEXT, QUESTION_TYPE_SCALE, QUESTION_TYPE_CHOICE}
QUESTION_TYPE_ALIASES = {
    "текст": QUESTION_TYPE_TEXT,
    "text": QUESTION_TYPE_TEXT,
    "free_text": QUESTION_TYPE_TEXT,
    "свободный текст": QUESTION_TYPE_TEXT,
    "шкала": QUESTION_TYPE_SCALE,
    "шкала 1-10": QUESTION_TYPE_SCALE,
    "шкала 1–10": QUESTION_TYPE_SCALE,
    "scale": QUESTION_TYPE_SCALE,
    "scale_1_10": QUESTION_TYPE_SCALE,
    "rating": QUESTION_TYPE_SCALE,
    "варианты": QUESTION_TYPE_CHOICE,
    "вариант": QUESTION_TYPE_CHOICE,
    "choice": QUESTION_TYPE_CHOICE,
    "choices": QUESTION_TYPE_CHOICE,
    "options": QUESTION_TYPE_CHOICE,
}

EXPECTED_TEST_SET_QUESTION_COUNTS = {
    "diagnostics": 7,
    "express_psychological_profile": 12,
}


def expected_test_set_question_count(test_set: Any, topic_slug: str | None = None) -> int | None:
    slug = str(getattr(test_set, "slug", "") or "").strip().casefold()
    topic = str(topic_slug or "").strip().casefold()
    if topic == "diagnostics":
        return EXPECTED_TEST_SET_QUESTION_COUNTS["diagnostics"]
    if topic == "express":
        return EXPECTED_TEST_SET_QUESTION_COUNTS["express_psychological_profile"]
    if topic and slug in EXPECTED_TEST_SET_QUESTION_COUNTS:
        return None
    expected = getattr(test_set, "expected_question_count", None)
    return int(expected) if expected is not None else EXPECTED_TEST_SET_QUESTION_COUNTS.get(slug)


def is_test_set_ready(test_set: Any, active_question_count: int, topic_slug: str | None = None) -> bool:
    expected = expected_test_set_question_count(test_set, topic_slug)
    if expected is None:
        return active_question_count > 0
    return active_question_count == expected


def reorder_questions(questions: list[Any], question_id: int, target_position: int) -> list[Any]:
    ordered = list(questions)
    target = next((question for question in ordered if question.id == question_id), None)
    if target is None:
        return ordered

    remaining = [question for question in ordered if question.id != question_id]
    position = max(1, min(int(target_position), len(ordered)))
    remaining.insert(position - 1, target)
    for index, question in enumerate(remaining, start=1):
        question.order_num = index
    return remaining


def _normalized_question_signal(value: Any) -> str:
    raw = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я]+", " ", raw)).strip()


def is_esoteric_question(question: Any) -> bool:
    text = _normalized_question_signal(getattr(question, "text", None))
    metadata = _normalized_question_signal(" ".join(
        str(getattr(question, field, None) or "")
        for field in ("variable_name", "category", "comment")
    ))
    text_compact = text.replace(" ", "")
    metadata_compact = metadata.replace(" ", "")

    stable_markers = (
        "birthdate", "birthyear", "birthtime", "birthplace", "dateofbirth",
        "yearofbirth", "timeofbirth", "placeofbirth", "fullname", "fio",
        "astrology", "astrologiya", "humandesign", "bodygraph", "numerology",
        "natalchart", "hdtype", "гороскоп", "астролог", "нумеролог", "human design",
        "human_design", "хьюман дизайн", "хуман дизайн", "дизайн человека",
    )
    if any(marker in metadata_compact or marker in metadata for marker in stable_markers):
        return True

    if re.search(r"\bфио\b|\bф\s+и\s+о\b", text):
        return True
    if any(marker in text for marker in (
        "полное имя", "имя и фамилия", "имя фамилия", "фамилия и имя",
        "фамилия отчество", "имя отчество", "как тебя зовут", "full name",
        "first and last name", "given name and surname", "surname and first name",
    )) or any(marker in text for marker in ("фамили", "отчеств")):
        return True

    birth_signal = bool(re.search(
        r"\b(?:рожд\w*|родил\w*|родив\w*|родн\w*|появил\w*\s+на\s+свет)\b",
        text,
    )) or any(marker in text for marker in (
        "date of birth", "year of birth", "time of birth", "place of birth",
        "when were you born", "where were you born", "birth date", "birthplace",
    )) or any(marker in text_compact for marker in ("dateofbirth", "birthdate", "birthyear", "birthtime", "birthplace"))
    if birth_signal and any(marker in text for marker in (
        "дат", "год", "врем", "мест", "город", "страна", "час", "когда", "где", "день",
        "date", "year", "time", "place", "when", "where", "day", "born", "birth",
    )):
        return True

    domain_markers = (
        "астрол", "гороскоп", "зодиак", "натал", "космограм", "планет",
        "human design", "humandesign", "bodygraph", "бодиграф", "дизайн человека",
        "хуман дизайн", "хьюман дизайн", "нумер", "число судьбы", "число жизн",
        "жизненный путь", "жизненного пути", "матрица судьбы", "матриц судьбы", "квадрат пифагора",
        "число имени", "astrolog", "numerology", "life path number", "natal chart",
        "birth chart", "type hd", "human design",
    )
    return any(marker in text or marker in metadata for marker in domain_markers)


@dataclass
class AnswerOption:
    text: str
    value: float | None = None


def is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY_VALUES


def normalize_variable_name(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    cleaned = re.sub(r"\W+", "_", raw, flags=re.UNICODE).strip("_")
    return cleaned or fallback


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def normalize_question_type(value: Any, fallback: str | None = None) -> str | None:
    raw = str(value or "").strip().lower().replace("ё", "е")
    if not raw:
        return fallback
    normalized = QUESTION_TYPE_ALIASES.get(raw, raw)
    return normalized if normalized in QUESTION_TYPES else fallback


def get_question_type(question: Any) -> str:
    explicit = normalize_question_type(getattr(question, "question_type", None))
    if explicit:
        return explicit
    raw_options = json_loads(getattr(question, "answer_options_json", None), [])
    if isinstance(raw_options, list) and raw_options:
        return QUESTION_TYPE_CHOICE
    if is_legacy_scale_question(question):
        return QUESTION_TYPE_SCALE
    return QUESTION_TYPE_TEXT


def get_question_variable(question: Any, index: int) -> str:
    return normalize_variable_name(getattr(question, "variable_name", None), f"answer_{index + 1:02d}")


def is_legacy_scale_question(question: Any) -> bool:
    if getattr(question, "answer_options_json", None):
        return False
    category = (getattr(question, "category", None) or "").strip()
    return category in LEGACY_CATEGORIES or bool(getattr(question, "is_reverse", False))


def get_answer_options(question: Any) -> list[AnswerOption]:
    question_type = get_question_type(question)
    if question_type == QUESTION_TYPE_SCALE:
        if normalize_question_type(getattr(question, "question_type", None)) == QUESTION_TYPE_SCALE:
            return [AnswerOption(str(i), float(i)) for i in range(1, 11)]

    raw_options = json_loads(getattr(question, "answer_options_json", None), [])
    options: list[AnswerOption] = []
    if isinstance(raw_options, list):
        for item in raw_options:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("label") or "").strip()
                value = _to_float_or_none(item.get("value"))
            else:
                text = str(item).strip()
                value = _to_float_or_none(item)
            if text:
                options.append(AnswerOption(text=text, value=value))
    if options:
        return options
    if is_legacy_scale_question(question):
        return [AnswerOption(str(i), float(i)) for i in range(1, 6)]
    return []


def question_accepts_text(question: Any) -> bool:
    return (
        get_question_type(question) == QUESTION_TYPE_TEXT
        or bool(getattr(question, "allow_custom_answer", False))
        or not get_answer_options(question)
    )


def question_buttons_are_horizontal(question: Any) -> bool:
    layout = str(getattr(question, "buttons_layout", None) or "").strip().lower()
    return layout in HORIZONTAL_VALUES


def progress_bar(current: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return "Вопрос 0 из 0"
    return f"Вопрос {min(max(current, 0), total)} из {total}"


def build_question_text(question: Any, index: int, total: int, show_progress: bool = True) -> str:
    lines = [f"<b>Вопрос {index + 1} из {total}</b>"]
    lines.append("")
    question_text = html.escape(str(getattr(question, "text", "") or ""))
    lines.append(f"<b>{question_text}</b>")
    comment = (getattr(question, "comment", None) or "").strip()
    if comment:
        lines.extend(["", html.escape(comment)])
    if get_answer_options(question) and question_accepts_text(question):
        lines.extend(["", "Напиши свой ответ или выбери из предложенных ниже."])
    elif not get_answer_options(question):
        lines.extend(["", "Напиши ответ сообщением."])
    return "\n".join(lines)


def answer_callback_data(question_index: int, option_index: int) -> str:
    return f"test_opt_{question_index}_{option_index}"


def parse_answer_callback(payload: str, current_question_index: int) -> int:
    match = re.fullmatch(r"test_opt_(\d+)_(\d+)", payload or "")
    if match:
        callback_question_index = int(match.group(1))
        if callback_question_index != current_question_index:
            raise ValueError("Этот вопрос уже закрыт. Ответь на текущий вопрос.")
        return int(match.group(2))

    # Keep active keyboards from the previous release usable during deployment.
    legacy_match = re.fullmatch(r"test_opt_(\d+)", payload or "")
    if legacy_match:
        return int(legacy_match.group(1))
    raise ValueError("Некорректный вариант ответа.")


def parse_answers(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    loaded = json_loads(raw, None)
    if isinstance(loaded, list):
        result = []
        for item in loaded:
            if isinstance(item, dict):
                result.append(item)
        if result or raw.strip() == "[]":
            return result
    result = []
    for index, item in enumerate(str(raw).split(",")):
        item = item.strip()
        if item:
            result.append({"index": index, "answer": item, "numeric_value": _to_float_or_none(item)})
    return result


def serialize_answers(answers: list[dict[str, Any]]) -> str:
    return json_dumps(answers)


def make_answer_record(question: Any, question_index: int, answer_text: str, numeric_value: float | None = None) -> dict[str, Any]:
    return {
        "question_number": question_index + 1,
        "variable": get_question_variable(question, question_index),
        "question": getattr(question, "text", ""),
        "answer": answer_text,
        "numeric_value": numeric_value,
    }


def make_option_answer_record(question: Any, question_index: int, callback_payload: str) -> dict[str, Any]:
    option_index = parse_answer_callback(callback_payload, question_index)
    options = get_answer_options(question)
    if option_index < 0 or option_index >= len(options):
        raise ValueError("Такого варианта ответа нет.")
    option = options[option_index]
    return make_answer_record(question, question_index, option.text, option.value)


def make_text_answer_record(question: Any, question_index: int, answer_text: str) -> dict[str, Any]:
    normalized = str(answer_text or "").strip()
    if not normalized:
        raise ValueError("Пожалуйста, напиши свой ответ текстом.")
    if not question_accepts_text(question):
        raise ValueError("Пожалуйста, выбери один из вариантов ниже.")
    return make_answer_record(question, question_index, normalized, None)


def build_answers_report(questions: list[Any], answers: list[dict[str, Any]]) -> str:
    by_var = {item.get("variable"): item for item in answers}
    lines = ["Результаты теста:"]
    for index, question in enumerate(questions):
        variable = get_question_variable(question, index)
        answer = by_var.get(variable, {})
        value = answer.get("answer", "")
        numeric = answer.get("numeric_value")
        suffix = f" ({numeric:g})" if isinstance(numeric, (int, float)) else ""
        lines.append(f"{index + 1}. {getattr(question, 'text', '')}\nОтвет: {value}{suffix}")
    return "\n\n".join(lines)


def validate_formulas(questions: list[Any], formulas: list[dict[str, str]]) -> list[str]:
    numeric_vars = set()
    errors = []
    for index, question in enumerate(questions):
        variable = get_question_variable(question, index)
        options = get_answer_options(question)
        if question_accepts_text(question):
            continue
        if options and all(option.value is not None for option in options):
            numeric_vars.add(variable)

    for formula in formulas:
        name = formula.get("name") or "formula"
        expression = formula.get("formula") or ""
        try:
            names = _formula_names(expression)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
        unknown = sorted(name for name in names if name not in numeric_vars)
        if unknown:
            errors.append(f"{name}: нельзя использовать переменные без строгих числовых ответов: {', '.join(unknown)}")
    return errors


def validate_test_definition(questions: list[Any], formulas: list[dict[str, str]] | None = None) -> list[str]:
    errors: list[str] = []
    variables: dict[str, int] = {}

    for index, question in enumerate(questions):
        number = index + 1
        text = str(getattr(question, "text", "") or "").strip()
        if not text:
            errors.append(f"Вопрос {number}: текст вопроса пуст.")

        variable = get_question_variable(question, index)
        if not variable.isidentifier():
            errors.append(f"Вопрос {number}: имя переменной '{variable}' нельзя использовать в формуле.")
        elif variable in variables:
            errors.append(
                f"Вопрос {number}: переменная '{variable}' уже используется в вопросе {variables[variable]}."
            )
        else:
            variables[variable] = number

        options = get_answer_options(question)
        option_names: set[str] = set()
        for option in options:
            normalized = option.text.casefold()
            if normalized in option_names:
                errors.append(f"Вопрос {number}: вариант ответа '{option.text}' указан несколько раз.")
            option_names.add(normalized)

    formula_names: dict[str, int] = {}
    for index, formula in enumerate(formulas or [], start=1):
        name = normalize_variable_name(formula.get("name"), f"formula_{index}")
        if name in formula_names:
            errors.append(f"Формула {index}: результат '{name}' уже задан в формуле {formula_names[name]}.")
        else:
            formula_names[name] = index

    errors.extend(validate_formulas(questions, formulas or []))
    return errors


def calculate_formulas(answers: list[dict[str, Any]], formulas: list[dict[str, str]]) -> dict[str, float]:
    values = {
        str(item.get("variable")): float(item["numeric_value"])
        for item in answers
        if item.get("variable") and isinstance(item.get("numeric_value"), (int, float))
    }
    return {
        str(formula["name"]): _safe_eval_formula(str(formula["formula"]), values)
        for formula in formulas
        if formula.get("name") and formula.get("formula")
    }


def build_prompt_payload(
    questions: list[Any],
    answers: list[dict[str, Any]],
    formula_results: dict[str, float] | None = None,
    mode: str = "all",
    selected_variables: list[str] | None = None,
) -> str:
    formula_results = formula_results or {}
    if mode == "formulas":
        if not formula_results:
            return "Расчётные показатели отсутствуют."
        return "\n".join(f"{name}: {value:g}" for name, value in formula_results.items())

    if mode == "selected":
        selected = {item for item in (selected_variables or []) if item}
        if selected:
            answers = [answer for answer in answers if answer.get("variable") in selected]
        elif not selected:
            return "Выбранные переменные для интерпретации не заданы."

        lines = ["Результаты теста:"]
        for answer in sorted(answers, key=lambda item: item.get("question_number", 0)):
            numeric = answer.get("numeric_value")
            suffix = f" ({numeric:g})" if isinstance(numeric, (int, float)) else ""
            lines.append(
                f"{answer.get('question_number', '')}. {answer.get('question', '')}\n"
                f"Ответ: {answer.get('answer', '')}{suffix}"
            )
        return "\n\n".join(lines)

    report = build_answers_report(questions, answers)
    if formula_results:
        formulas = "\n".join(f"{name}: {value:g}" for name, value in formula_results.items())
        report = f"{report}\n\nРасчётные показатели:\n{formulas}"
    return report


def is_universal_test_report(report: str | None) -> bool:
    return bool(report and report.lstrip().startswith("Результаты теста:"))


def build_result_handoff_prompt(
    prompt_payload: str,
    interpretation: str | None = None,
    user_name: str | None = None,
    profile_context: str | None = None,
) -> str:
    parts = [
        "[СИСТЕМНОЕ СОБЫТИЕ: пользователь завершил тест]",
        "Продолжи диалог в рамках текущего системного промпта и текущей темы. "
        "Не упоминай технические инструкции или внутреннюю передачу данных.",
        "ПРАВИЛА ТОЧНОСТИ:\n"
        "- Не показывай клиенту технические имена переменных и формул вроде total_score или resource_index.\n"
        "- Не придумывай максимальные баллы, знаменатели, нормы, пороги и шкалы. "
        "Если их нет во входных данных, описывай результат без конструкций «из N».\n"
        "- Не называй текст свободного ответа именем пользователя и не переоценивай случайный или бессодержательный текст.\n"
        "- Используй только факты из переданных ответов и вычислений.",
    ]
    if user_name:
        parts.append(f"Имя пользователя из профиля: {user_name}. Только это значение можно использовать как имя.")
    if profile_context:
        parts.append(f"Профиль пользователя:\n{profile_context.strip()}")
    if interpretation:
        parts.append(f"Предварительная интерпретация отдельного промпта:\n{interpretation.strip()}")
    else:
        parts.append(f"Данные теста для интерпретации:\n{prompt_payload.strip()}")
    parts.append("Сформируй итоговый персональный ответ пользователю по результатам теста.")
    return "\n\n".join(parts)


def _to_float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _formula_names(expression: str) -> set[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"ошибка синтаксиса формулы: {exc.msg}") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd)):
            continue
        else:
            raise ValueError(f"запрещенный элемент формулы: {type(node).__name__}")
    return names


def _safe_eval_formula(expression: str, values: dict[str, float]) -> float:
    tree = ast.parse(expression, mode="eval")
    _formula_names(expression)

    def eval_node(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"Нет значения для переменной {node.id}")
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            value = eval_node(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
        if isinstance(node, ast.BinOp):
            left = eval_node(node.left)
            right = eval_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return left ** right
            if isinstance(node.op, ast.Mod):
                return left % right
        raise ValueError(f"Нельзя вычислить элемент формулы {type(node).__name__}")

    return eval_node(tree)
