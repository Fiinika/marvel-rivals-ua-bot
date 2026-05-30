from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.collectors.base import CollectorDefinition
from services.i18n import t


class ModerationCallback(CallbackData, prefix="submission"):
    action: str
    submission_id: int


class CollectorCallback(CallbackData, prefix="collector"):
    collector_id: str


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


def collector_source_keyboard(collectors: list[CollectorDefinition]):
    builder = InlineKeyboardBuilder()
    for collector in collectors:
        builder.button(
            text=collector.button_text,
            callback_data=CollectorCallback(collector_id=collector.collector_id),
        )
    builder.adjust(1)
    return builder.as_markup()
