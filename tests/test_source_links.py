"""Tests for keeping a source post's own links: only trusted sources, only
allowlisted destinations, shorteners judged by where they actually go."""

from __future__ import annotations

import asyncio

import httpx

from services import source_links
from services.gemini import GeminiDraftInput, _append_extra_links
from services.source_links import collect_publishable_links


class _FakeResponse:
    def __init__(self, status_code: int, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


class _FakeClient:
    def __init__(self, redirects: dict[str, object], calls: list[str]) -> None:
        self._redirects = redirects
        self._calls = calls

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def head(self, url: str):
        self._calls.append(url)
        result = self._redirects.get(url)
        if isinstance(result, Exception):
            raise result
        return result or _FakeResponse(404)


def _run(monkeypatch, text: str, *, source_type: str = "bluesky", redirects=None, article_url: str = ""):
    calls: list[str] = []
    monkeypatch.setattr(
        source_links.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(redirects or {}, calls),
    )
    links = asyncio.run(
        collect_publishable_links(text, source_type=source_type, article_url=article_url)
    )
    return links, calls


def test_allowlisted_link_is_kept_without_any_request(monkeypatch) -> None:
    links, calls = _run(monkeypatch, "Watch it here: https://www.twitch.tv/marvelrivals now!")

    assert links == [("Twitch", "https://www.twitch.tv/marvelrivals")]
    assert calls == []  # a direct allowlisted host needs no resolution


def test_shortener_is_resolved_and_kept_when_it_lands_somewhere_allowed(monkeypatch) -> None:
    # The real case: the official account posts a bit.ly link to its Spotify playlist.
    links, calls = _run(
        monkeypatch,
        "Add it to your rotation: https://bit.ly/3RSAwZl",
        redirects={"https://bit.ly/3RSAwZl": _FakeResponse(301, "https://open.spotify.com/playlist/abc")},
    )

    assert links == [("Spotify", "https://open.spotify.com/playlist/abc")]
    assert calls == ["https://bit.ly/3RSAwZl"]


def test_shortener_pointing_somewhere_unlisted_is_dropped(monkeypatch) -> None:
    links, _calls = _run(
        monkeypatch,
        "Free skins here https://bit.ly/scam",
        redirects={"https://bit.ly/scam": _FakeResponse(301, "https://totally-legit-skins.example.com/claim")},
    )

    assert links == []


def test_only_one_redirect_hop_is_followed(monkeypatch) -> None:
    # A shortener that redirects to another shortener is not chased further.
    links, calls = _run(
        monkeypatch,
        "https://bit.ly/a",
        redirects={
            "https://bit.ly/a": _FakeResponse(301, "https://tinyurl.com/b"),
            "https://tinyurl.com/b": _FakeResponse(301, "https://www.twitch.tv/x"),
        },
    )

    assert links == []
    assert calls == ["https://bit.ly/a"]


def test_untrusted_sources_never_keep_links(monkeypatch) -> None:
    # Reddit and RivalSkins carry user-submitted leak content — the likeliest place
    # for a hostile link — so nothing is kept even from an allowlisted host.
    for source_type in ("reddit", "rivalskins", "official_marvel_rivals", "youtube", "wiki_facts"):
        links, calls = _run(
            monkeypatch,
            "See https://www.twitch.tv/marvelrivals",
            source_type=source_type,
        )
        assert links == [], source_type
        assert calls == [], source_type


def test_unlisted_host_is_dropped(monkeypatch) -> None:
    links, _calls = _run(monkeypatch, "Check https://evil.example.com/free-units")

    assert links == []


def test_lookalike_host_is_not_treated_as_allowlisted(monkeypatch) -> None:
    for url in (
        "https://marvelrivals.com.attacker.example/x",
        "https://nottwitch.tv/x",
        "https://twitch.tv.evil.example/x",
    ):
        links, _calls = _run(monkeypatch, f"Look {url}")
        assert links == [], url


def test_http_links_to_allowed_hosts_are_upgraded(monkeypatch) -> None:
    links, _calls = _run(monkeypatch, "Watch http://www.twitch.tv/marvelrivals")

    assert links == [("Twitch", "https://www.twitch.tv/marvelrivals")]


def test_http_links_elsewhere_are_still_dropped(monkeypatch) -> None:
    links, _calls = _run(monkeypatch, "Deal here http://free-units.example.com/x")

    assert links == []


def test_shortener_resolving_to_http_is_upgraded(monkeypatch) -> None:
    # bit.ly really does hand back an http URL for a YouTube Short; dropping that
    # would lose a perfectly good link on a technicality.
    links, _calls = _run(
        monkeypatch,
        "https://bit.ly/4vGjcok",
        redirects={"https://bit.ly/4vGjcok": _FakeResponse(301, "http://youtube.com/shorts/CQOBa-OwJMQ")},
    )

    assert links == [("YouTube", "https://youtube.com/shorts/CQOBa-OwJMQ")]


def test_non_http_schemes_are_rejected(monkeypatch) -> None:
    for text in (
        "javascript:alert(1)//www.twitch.tv/x",
        "ftp://www.twitch.tv/x",
        "data:text/html;base64,AAAA",
    ):
        links, _calls = _run(monkeypatch, f"Look {text}")
        assert links == [], text


def test_the_source_url_itself_is_not_repeated(monkeypatch) -> None:
    post = "https://www.twitch.tv/marvelrivals and https://bsky.app/profile/x/post/1"
    links, _calls = _run(
        monkeypatch,
        post,
        article_url="https://bsky.app/profile/x/post/1",
    )

    assert links == [("Twitch", "https://www.twitch.tv/marvelrivals")]


def test_trailing_punctuation_is_trimmed(monkeypatch) -> None:
    links, _calls = _run(monkeypatch, "Watch (https://www.twitch.tv/marvelrivals).")

    assert links == [("Twitch", "https://www.twitch.tv/marvelrivals")]


def test_at_most_two_links_are_kept(monkeypatch) -> None:
    text = " ".join(
        [
            "https://www.twitch.tv/a",
            "https://www.youtube.com/watch?v=b",
            "https://discord.gg/c",
        ]
    )
    links, _calls = _run(monkeypatch, text)

    assert len(links) == 2


def test_a_failing_shortener_is_simply_dropped(monkeypatch) -> None:
    links, _calls = _run(
        monkeypatch,
        "https://bit.ly/down",
        redirects={"https://bit.ly/down": httpx.ConnectError("boom")},
    )

    assert links == []


# --- rendering -----------------------------------------------------------------


def _draft_input(links: tuple[tuple[str, str], ...]) -> GeminiDraftInput:
    return GeminiDraftInput(
        title="t",
        article_url="https://bsky.app/x",
        article_date_display=None,
        datetime_notes=None,
        body_text="b",
        source_type="bluesky",
        source_name="Bluesky Marvel Rivals",
        extra_links=links,
    )


def test_links_are_appended_after_the_draft() -> None:
    result = _append_extra_links("Готуйтеся до нового кліпу.", _draft_input((("Spotify", "https://open.spotify.com/p"),)))

    assert result.splitlines()[0] == "Готуйтеся до нового кліпу."
    assert result.endswith("🔗 Spotify: https://open.spotify.com/p")


def test_no_links_leaves_the_draft_untouched() -> None:
    assert _append_extra_links("Текст", _draft_input(())) == "Текст"


def test_a_link_the_draft_already_contains_is_not_duplicated() -> None:
    draft = "Слухайте тут: https://open.spotify.com/p"
    assert _append_extra_links(draft, _draft_input((("Spotify", "https://open.spotify.com/p"),))) == draft
