from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete

from database import TestConfig, TestQuestion, async_session_maker, engine
from file_parser import parse_questions_file
from universal_tests import json_dumps, validate_test_definition


def _preview_question(data: dict) -> SimpleNamespace:
    return SimpleNamespace(**data)


async def seed(path: Path) -> tuple[int, int]:
    parsed = await parse_questions_file(io.BytesIO(path.read_bytes()), path.name)
    questions = parsed.get("questions", [])
    formulas = parsed.get("formulas", [])
    if not questions:
        raise ValueError("В файле не найдено вопросов.")
    errors = validate_test_definition([_preview_question(item) for item in questions], formulas)
    if errors:
        raise ValueError("Файл не прошёл проверку:\n- " + "\n- ".join(errors))

    async with async_session_maker() as session:
        async with session.begin():
            await session.execute(delete(TestQuestion))
            session.add_all(TestQuestion(sort_order=index, **item) for index, item in enumerate(questions))
            config = await session.get(TestConfig, 1)
            if config is None:
                config = TestConfig(id=1)
                session.add(config)
            config.formulas_json = json_dumps(formulas) if formulas else None
            config.formulas_enabled = bool(formulas)
    return len(questions), len(formulas)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and load a universal test.")
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        question_count, formula_count = await seed(args.file)
        print(f"Seed complete: questions={question_count} formulas={formula_count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
