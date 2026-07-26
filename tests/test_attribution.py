"""Tests for per-source attribution: the Gemini source/hashtag lines for
non-official sources, the shared allow-source-link predicate, and the generic
"Джерело: <name>" footer linkification (official linkification must stay intact)."""

from __future__ import annotations

from services.gemini import (
    GeminiDraftInput,
    _build_prompt,
    _ensure_required_metadata,
    _fallback_tags,
    _public_hashtag_line,
    _select_prompt_template,
    _source_attribution_line,
)
from services.post_footer import (
    OFFICIAL_SOURCE_ATTRIBUTION,
    format_post_html,
    submission_allows_source_link,
)


def _draft(source_type: str, source_name: str = "MR Bluesky") -> GeminiDraftInput:
    return GeminiDraftInput(
        title="title",
        article_url="https://example.com/1",
        article_date_display=None,
        datetime_notes=None,
        body_text="body",
        source_type=source_type,
        source_name=source_name,
    )


def test_non_official_attribution_uses_source_name() -> None:
    assert _source_attribution_line(_draft("bluesky", "MR Bluesky")) == "Джерело: MR Bluesky"


def test_official_attribution_is_unchanged() -> None:
    assert _source_attribution_line(_draft("official_marvel_rivals")) == OFFICIAL_SOURCE_ATTRIBUTION


def test_non_official_hashtags_drop_oficiyno() -> None:
    line = _public_hashtag_line(_draft("bluesky"))
    assert "#Офіційно" not in line
    assert "#MarvelRivalsUA" in line
    assert "#" in line.replace("#MarvelRivalsUA", "")  # also carries a topic tag now


def test_non_official_gets_topic_tags() -> None:
    draft = GeminiDraftInput(
        title="New trailer reveal",
        article_url="https://x",
        article_date_display=None,
        datetime_notes=None,
        body_text="watch the trailer",
        source_type="youtube",
        source_name="YT",
    )
    line = _public_hashtag_line(draft)
    assert "#Трейлер" in line
    assert "#MarvelRivalsUA" in line
    assert "#Офіційно" not in line


def _leak(title: str, body: str, source_type: str = "reddit") -> GeminiDraftInput:
    return GeminiDraftInput(
        title=title,
        article_url="https://reddit.com/x",
        article_date_display=None,
        datetime_notes=None,
        body_text=body,
        source_type=source_type,
        source_name="Reddit (витоки Marvel Rivals)",
    )


def test_leak_always_carries_the_rumour_tag() -> None:
    # Even when a topic tag matches, the marker stays — a label that shows up at
    # random is one readers cannot rely on.
    line = _public_hashtag_line(_leak("New skin leaked", "a datamined costume for Psylocke"))

    assert "#Чутки" in line
    assert "#Скіни" in line
    assert "#Офіційно" not in line


def test_leak_without_a_topic_match_is_not_called_an_announcement() -> None:
    line = _public_hashtag_line(_leak("Something odd in the files", "an unnamed asset showed up"))

    assert "#Чутки" in line
    assert "#Анонс" not in line


def test_rivalskins_is_tagged_as_a_rumour_too() -> None:
    assert "#Чутки" in _public_hashtag_line(_leak("Skin render", "upcoming render", source_type="rivalskins"))


def test_rumour_stored_tags_match_the_public_marker() -> None:
    tags = _fallback_tags(_leak("New skin leaked", "a datamined costume"))

    assert "чутки" in tags
    assert "marvelrivalsua" in tags


def test_non_rumour_sources_keep_the_announcement_fallback() -> None:
    # Bluesky/YouTube really do carry announcements, so their default is untouched.
    line = _public_hashtag_line(_draft("bluesky"))

    assert "#Анонс" in line
    assert "#Чутки" not in line
    assert "чутки" not in _fallback_tags(_draft("bluesky"))


def test_official_hashtags_are_unchanged_by_the_rumour_branch() -> None:
    line = _public_hashtag_line(_draft("official_marvel_rivals"))

    assert line.startswith("#MarvelRivalsUA #Офіційно")
    assert "#Чутки" not in line


def test_allow_source_link_predicate() -> None:
    assert submission_allows_source_link({"source_type": "official_marvel_rivals", "source_url": "https://x"})
    assert submission_allows_source_link({"source_type": "bluesky", "source_url": "https://x"})
    assert not submission_allows_source_link({"source_type": "", "source_url": "https://x"})  # user submission
    assert not submission_allows_source_link({"source_type": "bluesky", "source_url": ""})
    assert not submission_allows_source_link({})


def test_generic_source_line_is_linkified() -> None:
    html = format_post_html(
        "Текст новини.\n\nДжерело: MR Bluesky",
        source_url="https://bsky.app/profile/x",
        allow_source_link=True,
    )
    assert '<a href="https://bsky.app/profile/x">MR Bluesky</a>' in html
    assert "Джерело:" in html


def test_generic_source_not_linked_when_disallowed() -> None:
    html = format_post_html(
        "Джерело: MR Bluesky",
        source_url="https://bsky.app/profile/x",
        allow_source_link=False,
    )
    assert "<a" not in html
    assert "Джерело: MR Bluesky" in html


def test_generic_source_not_linked_without_url() -> None:
    html = format_post_html("Джерело: MR Bluesky", source_url="", allow_source_link=True)
    assert "<a" not in html


def test_official_source_still_linkified() -> None:
    html = format_post_html(
        f"Текст. {OFFICIAL_SOURCE_ATTRIBUTION}",
        source_url="https://www.marvelrivals.com/news/",
        allow_source_link=True,
    )
    assert '<a href="https://www.marvelrivals.com/news/">офіційному сайті</a>' in html


def test_text_without_source_line_is_plain_escaped() -> None:
    html = format_post_html("Просто текст без джерела", source_url="https://x", allow_source_link=True)
    assert "<a" not in html
    assert "Просто текст без джерела" in html


def test_official_source_selects_article_prompt() -> None:
    template = _select_prompt_template(_draft("official_marvel_rivals"))
    assert "Текст статті:" in template


def test_non_official_source_selects_short_form_prompt() -> None:
    template = _select_prompt_template(_draft("bluesky"))
    assert "короткий допис" in template.lower()
    assert "Текст статті:" not in template
    # The short-form post must be broken into paragraphs, not one solid block.
    assert "абзац" in template.lower()


def test_short_form_draft_has_no_publication_date_line() -> None:
    # Non-official (social/short-form) posts must NOT carry a bare "2026-06-12"
    # publication-date line mid-post — only the body, the source attribution and
    # the hashtags. The date stays available to admins via original_text.
    draft_input = GeminiDraftInput(
        title="Анонс",
        article_url="https://bsky.app/profile/x/post/1",
        article_date_display="2026-06-12",
        datetime_notes=None,
        body_text="body",
        source_type="bluesky",
        source_name="MR Bluesky",
    )

    result = _ensure_required_metadata("Текст допису про подію.", draft_input)

    assert "2026-06-12" not in result
    assert "Джерело: MR Bluesky" in result
    assert "#MarvelRivalsUA" in result


def test_reddit_source_prompt_gets_rumor_notice() -> None:
    prompt = _build_prompt(_draft("reddit", "Reddit (витоки Marvel Rivals)"))
    assert "НЕОФІЦІЙНИЙ" in prompt
    assert "датамайн" in prompt
    assert "{" not in prompt and "}" not in prompt


def test_non_rumor_source_has_no_rumor_notice() -> None:
    prompt = _build_prompt(_draft("bluesky", "MR Bluesky"))
    assert "НЕОФІЦІЙНИЙ" not in prompt
    assert "датамайн" not in prompt


def test_short_form_prompt_fills_all_placeholders() -> None:
    prompt = _build_prompt(_draft("bluesky", "MR Bluesky"))
    assert "MR Bluesky" in prompt
    assert "Джерело: MR Bluesky" in prompt  # per-source attribution line
    assert "{" not in prompt and "}" not in prompt  # every placeholder was filled
