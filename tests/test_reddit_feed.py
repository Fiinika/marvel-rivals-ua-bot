"""Tests for the Reddit leaks collector: search.rss Atom parsing (t3 id, comments
URL, selftext from div.md, full-res i.redd.it image vs preview-thumbnail fallback),
dedup, and the collector wiring (megathread keyword filter, DraftCandidate mapping)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from xml.sax.saxutils import escape

from services.collectors.base import ListingEntry
from services.collectors.reddit.collector import (
    RedditLeaksCollector,
    _is_excluded,
)
from services.collectors.reddit import feed_fetcher
from services.collectors.reddit.feed_fetcher import (
    RedditPost,
    _build_flair_query,
    _normalize_timestamp,
    parse_feed,
    upgrade_preview_images,
)


def _entry(
    post_id: str,
    title: str,
    *,
    content_html: str,
    published: str = "2026-06-10T21:00:00+00:00",
    thumb: str | None = None,
    link: str | None = None,
) -> str:
    link = link or f"https://www.reddit.com/r/MarvelRivalsLeaks/comments/{post_id[3:]}/x/"
    thumb_el = f'<media:thumbnail url="{escape(thumb)}"/>' if thumb else ""
    return (
        "<entry><author><name>/u/tester</name></author>"
        '<category term="MarvelRivalsLeaks" label="r/MarvelRivalsLeaks"/>'
        f'<content type="html">{escape(content_html)}</content>'
        f"<id>{post_id}</id>{thumb_el}"
        f'<link href="{escape(link)}" />'
        f"<published>{published}</published><title>{escape(title)}</title></entry>"
    )


def _feed(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">'
        "<title>r/MarvelRivalsLeaks</title>" + "".join(entries) + "</feed>"
    )


_IMAGE_CONTENT = (
    '<table><tr><td><a href="https://www.reddit.com/r/x/comments/abc/y/">'
    '<img src="https://preview.redd.it/abc.jpeg?width=640&crop=smart&s=sig" alt="t"/></a></td>'
    '<td><!-- SC_OFF --><div class="md"><p>Leak description here.</p></div><!-- SC_ON --> '
    'submitted by <a href="https://www.reddit.com/user/tester">/u/tester</a> <br/> '
    '<span><a href="https://i.redd.it/abc.jpeg">[link]</a></span> '
    '<span><a href="https://www.reddit.com/r/x/comments/abc/y/">[comments]</a></span></td></tr></table>'
)

_VIDEO_CONTENT = (
    '<table><tr><td><a href="https://youtu.be/vid"><img src="https://external-preview.redd.it/p.jpg?s=sig"/></a></td>'
    '<td><div class="md"><p>Skin trailer.</p></div> submitted by /u/tester <br/> '
    '<span><a href="https://youtu.be/vid">[link]</a></span></td></tr></table>'
)

_TEXT_CONTENT = '<!-- SC_OFF --><div class="md"><p>Just discussion, no image.</p></div><!-- SC_ON -->'


def test_parse_feed_image_post_uses_full_res_iredd() -> None:
    feed = _feed(
        _entry(
            "t3_abc",
            "Doctor Doom set for season 11!",
            content_html=_IMAGE_CONTENT,
            thumb="https://preview.redd.it/abc.jpeg?width=640&crop=smart&s=sig",
        )
    )
    posts = parse_feed(feed)

    assert len(posts) == 1
    post = posts[0]
    assert isinstance(post, RedditPost)
    assert post.post_id == "t3_abc"
    assert post.title == "Doctor Doom set for season 11!"
    assert post.web_url == "https://www.reddit.com/r/MarvelRivalsLeaks/comments/abc/x/"
    assert post.body_text == "Leak description here."
    assert post.created_at == "2026-06-10T21:00:00+00:00"
    # Full-resolution direct image (no query), preferred over the preview thumbnail.
    assert post.image_url == "https://i.redd.it/abc.jpeg"


def test_parse_feed_video_post_falls_back_to_thumbnail() -> None:
    feed = _feed(
        _entry(
            "t3_vid",
            "ESU Mantis skin trailer",
            content_html=_VIDEO_CONTENT,
            thumb="https://external-preview.redd.it/p.jpg?width=640&s=sig",
        )
    )
    post = parse_feed(feed)[0]

    # No i.redd.it image -> keep the signed preview thumbnail (query intact).
    assert post.image_url == "https://external-preview.redd.it/p.jpg?width=640&s=sig"
    assert post.body_text == "Skin trailer."


def test_parse_feed_rejects_host_spoofed_image_link() -> None:
    # A [link] whose host only CONTAINS "i.redd.it" (subdomain spoof) must NOT be
    # accepted as the photo; fall back to the genuine preview thumbnail.
    spoof = (
        '<table><tr><td><div class="md"><p>Leak.</p></div> submitted by /u/x <br/> '
        '<span><a href="https://i.redd.it.attacker.com/x.png">[link]</a></span></td></tr></table>'
    )
    feed = _feed(_entry("t3_spoof", "Spoof", content_html=spoof, thumb="https://preview.redd.it/safe.jpg?s=sig"))
    post = parse_feed(feed)[0]

    assert post.image_url == "https://preview.redd.it/safe.jpg?s=sig"


def test_parse_feed_rejects_substring_and_scheme_spoofs_with_no_thumb() -> None:
    for bad_href in (
        "https://evil.com/i.redd.it/x.jpg",  # path contains the host substring
        "javascript:alert(1)//i.redd.it/a.jpg",  # non-http scheme
    ):
        content = (
            '<table><tr><td><div class="md"><p>Leak.</p></div> '
            f'<span><a href="{escape(bad_href)}">[link]</a></span></td></tr></table>'
        )
        post = parse_feed(_feed(_entry("t3_b", "Bad", content_html=content)))[0]
        assert post.image_url is None, bad_href


def test_parse_feed_drops_non_reddit_thumbnail() -> None:
    feed = _feed(_entry("t3_t", "T", content_html=_TEXT_CONTENT, thumb="https://evil.com/x.jpg"))
    post = parse_feed(feed)[0]

    assert post.image_url is None


def test_parse_feed_text_post_has_no_image() -> None:
    feed = _feed(_entry("t3_txt", "Discussion", content_html=_TEXT_CONTENT))
    post = parse_feed(feed)[0]

    assert post.image_url is None
    assert post.body_text == "Just discussion, no image."


def test_parse_feed_dedups_by_id_and_skips_missing_id() -> None:
    feed = _feed(
        _entry("t3_dup", "First", content_html=_TEXT_CONTENT),
        _entry("t3_dup", "Duplicate", content_html=_TEXT_CONTENT),
    )
    assert len(parse_feed(feed)) == 1

    # An entry whose <id> is empty is skipped.
    no_id = _entry("t3_x", "No id", content_html=_TEXT_CONTENT).replace("<id>t3_x</id>", "<id></id>")
    assert parse_feed(_feed(no_id)) == []


def test_parse_feed_invalid_xml_returns_empty() -> None:
    assert parse_feed("not xml") == []
    assert parse_feed("") == []


def test_build_flair_query_ors_quoted_flairs() -> None:
    assert _build_flair_query(["Official News", "Reliable"]) == 'flair:"Official News" OR flair:"Reliable"'


def test_normalize_timestamp_handles_z() -> None:
    assert _normalize_timestamp("2026-06-10T21:00:00Z") == "2026-06-10T21:00:00+00:00"
    assert _normalize_timestamp("") is None


def test_is_excluded_matches_megathread() -> None:
    keywords = frozenset({"megathread"})
    assert _is_excluded("Marvel Rivals Season 8.5 Megathread", keywords) is True
    assert _is_excluded("Doctor Doom set for season 11!", keywords) is False


class _FakeResponse:
    def __init__(self, status_code: int, content_type: str) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeClient:
    """Stands in for httpx.AsyncClient during the preview-upgrade HEAD probe."""

    def __init__(self, responses: dict[str, object], calls: list[str]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def head(self, url: str):
        self._calls.append(url)
        result = self._responses.get(url, _FakeResponse(403, "text/html"))
        if isinstance(result, Exception):
            raise result
        return result


def _run_upgrade(monkeypatch, posts: list[RedditPost], responses: dict[str, object]) -> tuple[list[RedditPost], list[str]]:
    calls: list[str] = []
    monkeypatch.setattr(
        feed_fetcher.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(responses, calls),
    )
    return asyncio.run(upgrade_preview_images(posts)), calls


def _post(image_url: str | None, post_id: str = "t3_a") -> RedditPost:
    return RedditPost(
        post_id=post_id,
        web_url=f"https://www.reddit.com/r/MarvelRivalsLeaks/comments/{post_id[3:]}/x/",
        title="Leak",
        body_text="body",
        created_at="2026-07-25T20:00:00+00:00",
        image_url=image_url,
        author="/u/tester",
    )


def test_upgrade_preview_swaps_tiny_thumbnail_to_full_res(monkeypatch) -> None:
    posts = [_post("https://preview.redd.it/abc123.png?width=140&height=125&s=sig")]
    upgraded, calls = _run_upgrade(
        monkeypatch,
        posts,
        {"https://i.redd.it/abc123.png": _FakeResponse(200, "image/png")},
    )

    assert calls == ["https://i.redd.it/abc123.png"]
    # The signed 140px thumbnail is replaced by the full-resolution original.
    assert upgraded[0].image_url == "https://i.redd.it/abc123.png"
    # Every other field is carried over untouched.
    assert upgraded[0].post_id == posts[0].post_id
    assert upgraded[0].title == posts[0].title


def test_upgrade_preview_leaves_external_preview_alone(monkeypatch) -> None:
    # external-preview mirrors an image hosted ELSEWHERE: it has no i.redd.it twin,
    # so it must not even be probed.
    url = "https://external-preview.redd.it/Nx-9Zq.jpeg?width=640&s=sig"
    upgraded, calls = _run_upgrade(monkeypatch, [_post(url)], {})

    assert calls == []
    assert upgraded[0].image_url == url


def test_upgrade_preview_leaves_direct_and_missing_images_alone(monkeypatch) -> None:
    posts = [_post("https://i.redd.it/already.jpeg", "t3_a"), _post(None, "t3_b")]
    upgraded, calls = _run_upgrade(monkeypatch, posts, {})

    assert calls == []
    assert [p.image_url for p in upgraded] == ["https://i.redd.it/already.jpeg", None]


def test_upgrade_preview_keeps_thumbnail_when_twin_is_missing(monkeypatch) -> None:
    # Video/gallery previews have no i.redd.it original — Reddit answers 403/404.
    url = "https://preview.redd.it/novideo.jpg?width=140&s=sig"
    upgraded, calls = _run_upgrade(monkeypatch, [_post(url)], {})

    assert calls == ["https://i.redd.it/novideo.jpg"]
    assert upgraded[0].image_url == url


def test_upgrade_preview_rejects_non_image_response(monkeypatch) -> None:
    url = "https://preview.redd.it/page.jpg?s=sig"
    upgraded, _calls = _run_upgrade(
        monkeypatch,
        [_post(url)],
        {"https://i.redd.it/page.jpg": _FakeResponse(200, "text/html; charset=utf-8")},
    )

    assert upgraded[0].image_url == url


def test_upgrade_preview_survives_network_error(monkeypatch) -> None:
    url = "https://preview.redd.it/flaky.jpg?s=sig"
    upgraded, _calls = _run_upgrade(
        monkeypatch,
        [_post(url)],
        {"https://i.redd.it/flaky.jpg": feed_fetcher.httpx.ConnectError("boom")},
    )

    assert upgraded[0].image_url == url


def test_upgrade_preview_skips_paths_that_are_not_plain_filenames(monkeypatch) -> None:
    # Anything but a single-segment image file name is left as-is rather than
    # guessed at, so a crafted path can never be turned into a fetched URL.
    for path in ("/../../evil.jpg", "/nested/dir/img.jpg", "/noextension", "/img.svg"):
        url = f"https://preview.redd.it{path}?s=sig"
        upgraded, calls = _run_upgrade(monkeypatch, [_post(url)], {})
        assert calls == [], path
        assert upgraded[0].image_url == url, path


def test_upgrade_preview_handles_a_mixed_batch(monkeypatch) -> None:
    posts = [
        _post("https://preview.redd.it/one.png?width=140&s=sig", "t3_a"),
        _post("https://external-preview.redd.it/two.jpg?width=640&s=sig", "t3_b"),
        _post("https://i.redd.it/three.jpeg", "t3_c"),
        _post("https://preview.redd.it/four.jpg?width=140&s=sig", "t3_d"),
    ]
    upgraded, calls = _run_upgrade(
        monkeypatch,
        posts,
        {"https://i.redd.it/one.png": _FakeResponse(200, "image/png")},
    )

    assert sorted(calls) == ["https://i.redd.it/four.jpg", "https://i.redd.it/one.png"]
    assert [p.image_url for p in upgraded] == [
        "https://i.redd.it/one.png",  # upgraded
        "https://external-preview.redd.it/two.jpg?width=640&s=sig",  # untouched
        "https://i.redd.it/three.jpeg",  # already full-res
        "https://preview.redd.it/four.jpg?width=140&s=sig",  # twin missing, kept
    ]


class _StubFetcher:
    def __init__(self, posts: list[RedditPost]) -> None:
        self._posts = posts

    async def fetch_recent_posts(self) -> list[RedditPost]:
        return self._posts


def _collector(posts: list[RedditPost]) -> RedditLeaksCollector:
    config = SimpleNamespace(
        reddit_subreddit="MarvelRivalsLeaks",
        reddit_flairs=("Official News", "Reliable", "Confirmed"),
        reddit_exclude_keywords=frozenset({"megathread"}),
    )
    collector = RedditLeaksCollector(config=config, db=None, bot=None)
    collector.fetcher = _StubFetcher(posts)
    return collector


def test_fetch_listing_filters_megathread() -> None:
    posts = [
        RedditPost("t3_mt", "https://reddit/x", "Season 8.5 Megathread", "body", "2026-06-12T00:00:00+00:00", None, "/u/a"),
        RedditPost("t3_leak", "https://reddit/y", "Doctor Doom set for season 11!", "body", "2026-06-10T00:00:00+00:00", "https://i.redd.it/d.jpg", "/u/b"),
    ]
    entries = asyncio.run(_collector(posts).fetch_listing())

    ids = [e.dedup_key for e in entries]
    assert ids == ["t3_leak"]


def test_parse_entry_maps_post_to_candidate() -> None:
    post = RedditPost(
        post_id="t3_leak",
        web_url="https://www.reddit.com/r/MarvelRivalsLeaks/comments/leak/x/",
        title="Doctor Doom set for season 11!",
        body_text="Datamine context.",
        created_at="2026-06-10T21:00:00+00:00",
        image_url="https://i.redd.it/d.jpeg",
        author="/u/tester",
    )
    entry = ListingEntry(dedup_key=post.post_id, payload=post)

    candidate = asyncio.run(_collector([]).parse_entry(entry))

    assert candidate.source_id == "t3_leak"
    assert candidate.source_url == post.web_url
    assert candidate.title == "Doctor Doom set for season 11!"
    assert candidate.body_text == "Datamine context."
    assert candidate.source_name == "Reddit (витоки Marvel Rivals)"
    assert candidate.has_media is True
    assert candidate.media_url == "https://i.redd.it/d.jpeg"
    assert candidate.media_type == "photo"
    assert candidate.article_date_display == "2026-06-10"
