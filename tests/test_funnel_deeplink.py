import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/vitaliy_funnel_deeplink.db")
sys.modules.setdefault(
    "vector_store",
    SimpleNamespace(get_collection=lambda: None, get_active_path=lambda: None),
)

from funnel import parse_start_action


class _Session:
    def __init__(self, user):
        self.user = user
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_):
        return self.user


class FunnelDeepLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_start_values(self):
        for value in ("funnel", "practice", "hypno", "hypnosis", "hypnosession", "meditation", "funnel_123"):
            self.assertEqual(parse_start_action(value)[0], "funnel")
        self.assertEqual(parse_start_action("12345"), ("referral", 12345))
        self.assertEqual(parse_start_action("unknown"), (None, None))

    async def test_start_funnel_opens_funnel_not_main_menu(self):
        import handlers

        user = SimpleNamespace(is_blocked=False, referrer_id=None, name="Tester", first_name="Tester", gender="male")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42, username="tester", first_name="Tester"),
            bot=SimpleNamespace(send_message=AsyncMock()),
            answer=AsyncMock()
        )
        with (
            patch("handlers.async_session_maker", return_value=_Session(user)),
            patch("funnel.async_session_maker", return_value=_Session(user)),
            patch("handlers.start_funnel_intro", new=AsyncMock()) as start_funnel,
            patch("handlers.get_content", new=AsyncMock(return_value=SimpleNamespace(text_content="Welcome", media=[]))) as get_content,
        ):
            await handlers.cmd_start(message, AsyncMock(), SimpleNamespace(args="funnel"))

        start_funnel.assert_awaited_once_with(message, bot=message.bot)
        get_content.assert_any_call("welcome_msg")


if __name__ == "__main__":
    unittest.main()
