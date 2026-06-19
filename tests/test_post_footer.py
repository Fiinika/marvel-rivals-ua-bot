"""Tests for the community-footer rendering, especially that the footer is located
by a structural sentinel — so an untrusted post body containing the visible footer
title can no longer suppress, hijack or forge the footer."""

from __future__ import annotations

from services.i18n import t
from services.post_footer import (
    _FOOTER_SENTINEL,
    format_community_footer_html,
    format_post_html,
)


CHAT_LINK = '<a href="https://t.me/UAMarvelRivalsChat">Чат</a>'
FOOTER_TITLE = t("post_footer.title")


def test_footer_is_appended_and_linkified() -> None:
    html = format_post_html("Свіжа новина.", include_community_footer=True)
    assert FOOTER_TITLE in html
    assert CHAT_LINK in html  # footer links are turned into anchors
    assert _FOOTER_SENTINEL not in html  # the marker never reaches output


def test_body_containing_footer_title_does_not_suppress_the_footer() -> None:
    # The attack: a feed body that embeds the visible footer title used to make the
    # old title-substring check think the footer was already present and skip it.
    malicious = f"Дивіться: {FOOTER_TITLE} (підробка) і ще текст."
    html = format_post_html(malicious, include_community_footer=True)

    # The real footer is still appended with working links...
    assert CHAT_LINK in html
    # ...and the title phrase now appears at least twice: the attacker's escaped copy
    # plus the genuine appended footer (i.e. it was NOT suppressed).
    assert html.count(FOOTER_TITLE) >= 2
    assert _FOOTER_SENTINEL not in html


def test_injected_sentinel_in_body_is_stripped() -> None:
    html = format_post_html(f"зло{_FOOTER_SENTINEL}текст", include_community_footer=True)
    assert _FOOTER_SENTINEL not in html  # an injected sentinel cannot survive
    assert CHAT_LINK in html  # and cannot move/suppress the real footer


def test_no_footer_when_not_requested() -> None:
    html = format_post_html("Просто текст.", include_community_footer=False)
    assert FOOTER_TITLE not in html
    assert _FOOTER_SENTINEL not in html


def test_community_footer_html_is_linkified_without_sentinel() -> None:
    # The album digest caption builds its own HTML via this helper.
    html = format_community_footer_html()
    assert CHAT_LINK in html
    assert FOOTER_TITLE in html
    assert _FOOTER_SENTINEL not in html
