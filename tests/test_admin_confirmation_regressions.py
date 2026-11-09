import json
import uuid
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery
from sqlalchemy import delete, select

from database import (
    BotConfig,
    Content,
    ContentMedia,
    SubscriptionPlan,
    TestSet,
    User,
    async_session_maker,
    init_db,
)
from automation_engine import AutomationHandler
import automation_admin
import funnel
import handlers
import test_set_admin_handlers


class AdminConfirmationRegressionTests(IsolatedAsyncioTestCase):
    admin_id = 982000101
    target_admin_id = 982000102
    handler_name = "admin_confirmation_regression_handler"
    content_key = "admin_confirmation_regression_content"
    funnel_media_id = "admin-confirmation-regression-funnel-media"
    content_media_id = "admin-confirmation-regression-content-media"
    test_set_slug = "admin_confirmation_regression_test_set"

    async def asyncSetUp(self):
        await init_db()
        self.storage = MemoryStorage()
        self.handler_name = f"admin_confirmation_regression_handler_{uuid.uuid4().hex}"
        self.content_key = f"admin_confirmation_regression_content_{uuid.uuid4().hex}"
        self.funnel_media_id = f"admin-confirmation-regression-funnel-media-{uuid.uuid4().hex}"
        self.content_media_id = f"admin-confirmation-regression-content-media-{uuid.uuid4().hex}"
        self.test_set_slug = f"admin_confirmation_regression_test_set_{uuid.uuid4().hex}"
        async with async_session_maker() as session:
            admin = await session.get(User, self.admin_id)
            if admin is None:
                admin = User(id=self.admin_id, first_name="Regression admin", is_admin=True)
                session.add(admin)
            else:
                admin.is_admin = True

            target = await session.get(User, self.target_admin_id)
            if target is None:
                target = User(id=self.target_admin_id, first_name="Target admin", is_admin=True)
                session.add(target)
            else:
                target.is_admin = True

            config = await session.get(BotConfig, 1)
            if config is None:
                config = BotConfig(id=1, webhook_secret="old-regression-secret")
                session.add(config)
            self.original_webhook_secret = config.webhook_secret
            config.webhook_secret = "old-regression-secret"

            handler = AutomationHandler(
                name=self.handler_name,
                event_name="regression_event",
                action_type="send_admin_message",
                action_payload_json=json.dumps({"text": "test"}),
                recipient_ids_json=json.dumps([self.target_admin_id]),
            )
            session.add(handler)

            plan = SubscriptionPlan(name="Regression plan", price=100)
            session.add(plan)

            test_set = TestSet(title="Regression test set", slug=self.test_set_slug)
            session.add(test_set)

            content = Content(key=self.content_key, text_content="Исходный текст")
            session.add(content)
            session.add(
                ContentMedia(
                    content_key=self.content_key,
                    file_type="photo",
                    file_id=self.content_media_id,
                )
            )

            funnel_content = await session.get(Content, "funnel_intro")
            if funnel_content is None:
                funnel_content = Content(key="funnel_intro", text_content="Интро")
                session.add(funnel_content)
            session.add(
                ContentMedia(
                    content_key="funnel_intro",
                    file_type="photo",
                    file_id=self.funnel_media_id,
                )
            )
            await session.commit()
            self.handler_id = handler.id
            self.plan_id = plan.id
            self.test_set_id = test_set.id

    async def asyncTearDown(self):
        async with async_session_maker() as session:
            await session.execute(delete(AutomationHandler).where(AutomationHandler.id == self.handler_id))
            await session.execute(delete(SubscriptionPlan).where(SubscriptionPlan.id == self.plan_id))
            await session.execute(delete(TestSet).where(TestSet.id == self.test_set_id))
            await session.execute(delete(ContentMedia).where(ContentMedia.file_id.in_((self.content_media_id, self.funnel_media_id))))
            await session.execute(delete(Content).where(Content.key == self.content_key))
            config = await session.get(BotConfig, 1)
            if config is not None:
                config.webhook_secret = self.original_webhook_secret
            await session.commit()
        await self.storage.close()

    def state(self) -> FSMContext:
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=99104, chat_id=self.admin_id, user_id=self.admin_id),
        )

    def callback(self, data: str):
        callback = MagicMock(spec=CallbackQuery)
        callback.data = data
        callback.from_user = SimpleNamespace(id=self.admin_id, first_name="Regression admin")
        callback.message = SimpleNamespace(
            chat=SimpleNamespace(id=self.admin_id),
            message_id=1,
            edit_text=AsyncMock(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            delete=AsyncMock(),
        )
        callback.answer = AsyncMock()
        callback.bot = AsyncMock()
        return callback

    @staticmethod
    def markup_callbacks(markup):
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]

    async def test_recipient_clear_ask_cancel_confirm_and_empty_list(self):
        ask = self.callback(f"automation_handler_recipients_clear_ask_{self.handler_id}")
        await automation_admin.callback_automation_handler_router(ask, self.state())
        callbacks = self.markup_callbacks(ask.message.edit_text.call_args.kwargs["reply_markup"])
        self.assertIn(f"automation_handler_recipients_clear_confirm_{self.handler_id}", callbacks)

        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, self.handler_id)
            self.assertEqual(json.loads(handler.recipient_ids_json), [self.target_admin_id])

        cancel = self.callback(f"automation_handler_recipients_{self.handler_id}")
        await automation_admin.callback_automation_handler_router(cancel, self.state())
        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, self.handler_id)
            self.assertEqual(json.loads(handler.recipient_ids_json), [self.target_admin_id])

        confirm = self.callback(f"automation_handler_recipients_clear_confirm_{self.handler_id}")
        await automation_admin.callback_automation_handler_router(confirm, self.state())
        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, self.handler_id)
            self.assertEqual(json.loads(handler.recipient_ids_json), [])

        empty = self.callback(f"automation_handler_recipients_clear_ask_{self.handler_id}")
        await automation_admin.callback_automation_handler_router(empty, self.state())
        empty.answer.assert_any_call("Список получателей уже пуст.")
        async with async_session_maker() as session:
            handler = await session.get(AutomationHandler, self.handler_id)
            self.assertEqual(json.loads(handler.recipient_ids_json), [])

    async def test_old_destructive_callbacks_only_show_confirmation(self):
        webhook = self.callback("admin_gen_webhook_secret")
        await handlers.admin_gen_webhook_secret(webhook, self.state())

        old_plan = self.callback(f"adm_del_plan_{self.plan_id}")
        await handlers.admin_delete_plan(old_plan)
        new_plan = self.callback(f"admin_delete_plan_{self.plan_id}")
        await handlers.admin_delete_plan_new(new_plan, self.state())

        test_set = self.callback(f"ts_delete_{self.test_set_id}")
        await test_set_admin_handlers.delete_or_archive_test_set(test_set, self.state())

        admin_delete = self.callback(f"del_admin_{self.target_admin_id}")
        await handlers.delete_admin_handler(admin_delete, AsyncMock())

        content_delete = self.callback(f"del_cont_media_main:{self.content_key}")
        await handlers.del_cont_media(content_delete, self.state())

        funnel_delete = self.callback("del_fcont_media_funnel_intro")
        await funnel.admin_del_fcont_media(funnel_delete, self.state())

        async with async_session_maker() as session:
            config = await session.get(BotConfig, 1)
            self.assertEqual(config.webhook_secret, "old-regression-secret")
            self.assertIsNotNone(await session.get(SubscriptionPlan, self.plan_id))
            self.assertIsNotNone(await session.get(TestSet, self.test_set_id))
            target = await session.get(User, self.target_admin_id)
            self.assertTrue(target.is_admin)
            content_media = (
                await session.execute(
                    select(ContentMedia).where(ContentMedia.file_id == self.content_media_id)
                )
            ).scalars().all()
            funnel_media = (
                await session.execute(
                    select(ContentMedia).where(ContentMedia.file_id == self.funnel_media_id)
                )
            ).scalars().all()
            self.assertEqual(len(content_media), 1)
            self.assertEqual(len(funnel_media), 1)

        self.assertIn(
            "confirmed_admin_gen_webhook_secret",
            self.markup_callbacks(webhook.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            f"confirmed_adm_del_plan_{self.plan_id}",
            self.markup_callbacks(old_plan.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            f"confirmed_admin_delete_plan_{self.plan_id}",
            self.markup_callbacks(new_plan.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            f"confirmed_ts_delete_{self.test_set_id}",
            self.markup_callbacks(test_set.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            f"confirmed_del_admin_{self.target_admin_id}",
            self.markup_callbacks(admin_delete.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            f"confirmed_del_cont_media_main:{self.content_key}",
            self.markup_callbacks(content_delete.message.edit_text.call_args.kwargs["reply_markup"]),
        )
        self.assertIn(
            "confirmed_del_fcont_media_funnel_intro",
            self.markup_callbacks(funnel_delete.message.edit_text.call_args.kwargs["reply_markup"]),
        )

    async def test_confirmed_webhook_route_executes_the_deferred_change(self):
        callback = self.callback("confirmed_admin_gen_webhook_secret")
        await handlers.confirmed_admin_gen_webhook_secret(callback, self.state())
        async with async_session_maker() as session:
            config = await session.get(BotConfig, 1)
            self.assertNotEqual(config.webhook_secret, "old-regression-secret")

    async def test_funnel_media_confirmation_handles_media_message_and_cancel(self):
        state = self.state()
        open_callback = self.callback("admin_fcont_funnel_intro")
        await funnel.admin_funnel_content_edit(open_callback, state)
        self.assertIsNotNone(await state.get_state())

        media_callback = self.callback("confirm_del_fcont_media_funnel_intro")
        media_callback.message = SimpleNamespace(
            chat=SimpleNamespace(id=self.admin_id),
            message_id=2,
            content_type="photo",
            photo=[SimpleNamespace(file_id=self.funnel_media_id)],
            edit_text=AsyncMock(),
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        await funnel.confirm_del_fcont_media(media_callback, state)

        media_callback.message.edit_text.assert_not_awaited()
        media_callback.message.delete.assert_awaited_once()
        media_callback.bot.send_message.assert_awaited_once()
        confirmation_markup = media_callback.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertIn(
            "confirmed_del_fcont_media_funnel_intro",
            self.markup_callbacks(confirmation_markup),
        )

        async with async_session_maker() as session:
            self.assertIsNotNone(
                (
                    await session.execute(
                        select(ContentMedia).where(ContentMedia.file_id == self.funnel_media_id)
                    )
                ).scalar_one_or_none()
            )

        cancel_callback = self.callback("admin_fcont_funnel_intro")
        await funnel.admin_funnel_content_edit(cancel_callback, state)
        async with async_session_maker() as session:
            self.assertIsNotNone(
                (
                    await session.execute(
                        select(ContentMedia).where(ContentMedia.file_id == self.funnel_media_id)
                    )
                ).scalar_one_or_none()
            )

        confirm_again = self.callback("confirm_del_fcont_media_funnel_intro")
        await funnel.confirm_del_fcont_media(confirm_again, state)
        confirmed = self.callback("confirmed_del_fcont_media_funnel_intro")
        with patch("funnel.show_admin_funnel_content", new_callable=AsyncMock) as show_content:
            await funnel.confirmed_admin_del_fcont_media(confirmed, state)
        show_content.assert_awaited_once_with(confirmed)
        async with async_session_maker() as session:
            self.assertIsNone(
                (
                    await session.execute(
                        select(ContentMedia).where(ContentMedia.file_id == self.funnel_media_id)
                    )
                ).scalar_one_or_none()
            )

    async def test_funnel_content_text_media_cancel_and_return(self):
        state = self.state()
        await state.set_state(funnel.FunnelAdminStates.edit_content)
        await state.update_data(content_key=self.content_key)
        text_message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="Новый текст",
            html_text="Новый текст",
            caption=None,
            photo=None,
            video=None,
            answer=AsyncMock(),
        )
        with patch("funnel.show_admin_funnel_content", new_callable=AsyncMock):
            await funnel.admin_funnel_content_save(text_message, state)

        async with async_session_maker() as session:
            content = await session.get(Content, self.content_key)
            self.assertEqual(content.text_content, "Новый текст")

        await state.set_state(funnel.FunnelAdminStates.edit_content)
        await state.update_data(content_key=self.content_key)
        photo_message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text=None,
            html_text=None,
            caption="Подпись фото",
            photo=[SimpleNamespace(file_id="regression-photo")],
            video=None,
            answer=AsyncMock(),
        )
        with patch("funnel.show_admin_funnel_content", new_callable=AsyncMock):
            await funnel.admin_funnel_content_save_media(photo_message, state)

        await state.set_state(funnel.FunnelAdminStates.edit_content)
        await state.update_data(content_key=self.content_key)
        video_message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text=None,
            html_text=None,
            caption="Подпись видео",
            photo=None,
            video=SimpleNamespace(file_id="regression-video"),
            answer=AsyncMock(),
        )
        with patch("funnel.show_admin_funnel_content", new_callable=AsyncMock):
            await funnel.admin_funnel_content_save_media(video_message, state)

        async with async_session_maker() as session:
            content = await session.get(Content, self.content_key)
            media = (
                await session.execute(
                    select(ContentMedia).where(ContentMedia.content_key == self.content_key)
                )
            ).scalars().all()
            self.assertEqual(content.text_content, "Подпись видео")
            self.assertEqual([(item.file_type, item.file_id) for item in media], [("video", "regression-video")])

        await state.set_state(funnel.FunnelAdminStates.edit_content)
        await state.update_data(content_key=self.content_key)
        cancel_message = SimpleNamespace(
            from_user=SimpleNamespace(id=self.admin_id),
            text="отмена",
            html_text="отмена",
            caption=None,
            photo=None,
            video=None,
            answer=AsyncMock(),
        )
        with patch("funnel.show_admin_funnel_content", new_callable=AsyncMock) as show_content:
            await funnel.admin_funnel_content_save(cancel_message, state)
        self.assertIsNone(await state.get_state())
        show_content.assert_awaited_once_with(cancel_message)

        async with async_session_maker() as session:
            content = await session.get(Content, self.content_key)
            media = (
                await session.execute(
                    select(ContentMedia).where(ContentMedia.content_key == self.content_key)
                )
            ).scalars().all()
            self.assertEqual(content.text_content, "Подпись видео")
            self.assertEqual(len(media), 1)
