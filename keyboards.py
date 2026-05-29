from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ModerationCallback(CallbackData, prefix="submission"):
    action: str
    submission_id: int


def moderation_keyboard(submission_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Approve",
        callback_data=ModerationCallback(action="approve", submission_id=submission_id),
    )
    builder.button(
        text="✏️ Edit",
        callback_data=ModerationCallback(action="edit", submission_id=submission_id),
    )
    builder.button(
        text="❌ Reject",
        callback_data=ModerationCallback(action="reject", submission_id=submission_id),
    )
    builder.adjust(3)
    return builder.as_markup()

