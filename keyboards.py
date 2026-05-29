from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.i18n import t


class ModerationCallback(CallbackData, prefix="submission"):
    action: str
    submission_id: int


def moderation_keyboard(submission_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("buttons.approve"),
        callback_data=ModerationCallback(action="approve", submission_id=submission_id),
    )
    builder.button(
        text=t("buttons.edit"),
        callback_data=ModerationCallback(action="edit", submission_id=submission_id),
    )
    builder.button(
        text=t("buttons.reject"),
        callback_data=ModerationCallback(action="reject", submission_id=submission_id),
    )
    builder.adjust(3)
    return builder.as_markup()
