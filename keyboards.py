from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.collectors.base import CollectorDefinition
from services.i18n import t


class ModerationCallback(CallbackData, prefix="submission"):
    action: str
    submission_id: int


class ModerationPartCallback(CallbackData, prefix="submission_part"):
    action: str
    submission_id: int
    part_index: int


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


def edit_part_keyboard(submission_id: int, part_count: int):
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for part_index in range(1, part_count + 1):
        row.append(
            InlineKeyboardButton(
                text=t("buttons.edit_part", part_index=part_index),
                callback_data=ModerationPartCallback(
                    action="select",
                    submission_id=submission_id,
                    part_index=part_index,
                ).pack(),
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text=t("buttons.add_part"),
                callback_data=ModerationPartCallback(
                    action="add",
                    submission_id=submission_id,
                    part_index=0,
                ).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def save_draft_keyboard(submission_id: int, part_index: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("buttons.save"),
                    callback_data=ModerationPartCallback(
                        action="save",
                        submission_id=submission_id,
                        part_index=part_index,
                    ).pack(),
                )
            ]
        ]
    )


def collector_source_keyboard(collectors: list[CollectorDefinition]):
    builder = InlineKeyboardBuilder()
    for collector in collectors:
        builder.button(
            text=collector.button_text,
            callback_data=CollectorCallback(collector_id=collector.collector_id),
        )
    builder.adjust(1)
    return builder.as_markup()
