"""Startup & Dispatcher Router Smoke Test.

Verifies that the exact set of routers used in main.py startup path can be
cleanly registered into an aiogram Dispatcher without RuntimeError (e.g. duplicate attachment).
"""

from __future__ import annotations

import unittest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

# Exact router imports from main.py
from funnel import router as funnel_router
from test_system_handlers import router as test_system_router
from topic_admin_handlers import router as topic_admin_router
from test_set_admin_handlers import router as test_set_admin_router
from followup_admin import router as followup_admin_router
from ai_logs_admin import router as ai_logs_admin_router
from automation_admin import router as automation_admin_router
from journal_admin_handlers import router as journal_admin_router
from journal_handlers import router as journal_router
from handlers import router as handlers_router


class StartupRouterSmokeTests(unittest.TestCase):
    """Verifies that all production routers attach to the Dispatcher without conflict."""

    def test_main_dispatcher_router_inclusion_smoke(self):
        """Constructs a fresh Dispatcher and includes all production routers in exact main.py order."""
        routers = [
            funnel_router,
            test_system_router,
            topic_admin_router,
            test_set_admin_router,
            followup_admin_router,
            ai_logs_admin_router,
            automation_admin_router,
            journal_admin_router,
            journal_router,
            handlers_router,
        ]

        # Reset parent routers before testing
        for r in routers:
            r._parent_router = None

        dp = Dispatcher(storage=MemoryStorage())

        # Include each router exactly as main.py does
        for r in routers:
            dp.include_router(r)

        # Assert all routers are registered as sub_routers of Dispatcher
        registered_sub_routers = dp.sub_routers
        self.assertEqual(len(registered_sub_routers), 10)

        for r in routers:
            self.assertIs(
                r._parent_router,
                dp,
                f"Router '{r.name}' parent router is not the Dispatcher!",
            )

        # Cleanup
        for r in routers:
            r._parent_router = None


if __name__ == "__main__":
    unittest.main()
