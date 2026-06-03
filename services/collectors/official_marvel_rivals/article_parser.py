from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from services.collectors.official_marvel_rivals.news_fetcher import NewsArticleSummary, fetch_html
from services.date_utils import ParsedArticleDate, parse_article_date
from services.media_parser import extract_article_media


logger = logging.getLogger(__name__)

MAX_ARTICLE_TEXT_LENGTH = 12000


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    canonical_url: str
    article_url: str
    raw_date: str | None
    date_info: ParsedArticleDate | None
    body_text: str
    raw_excerpt: str | None
    media_url: str | None
    media_urls: tuple[str, ...]
    media_type: str


class ArticleParser:
    def __init__(self, article_timezone: str) -> None:
        self.article_timezone = article_timezone

    async def fetch_and_parse(self, summary: NewsArticleSummary) -> ParsedArticle:
        html = await fetch_html(summary.article_url)
        if html is None:
            logger.warning("Using news-list fallback data for article %s", summary.article_url)
            return self.from_summary(summary)

        try:
            return self.parse(
                html=html,
                summary=summary,
            )
        except Exception:
            logger.exception("Failed to parse article page %s", summary.article_url)
            return self.from_summary(summary)

    def parse(self, *, html: str, summary: NewsArticleSummary) -> ParsedArticle:
        soup = BeautifulSoup(html, "html.parser")
        body_container = _select_body_container(soup)
        raw_date = _extract_article_date(soup) or summary.raw_date
        date_info = parse_article_date(raw_date, self.article_timezone)
        canonical_url = _canonical_article_url(soup, summary.article_url)
        title = _extract_title(soup) or summary.title
        body_text = _extract_body_text(body_container or soup)
        if not body_text and summary.raw_excerpt:
            body_text = summary.raw_excerpt

        body_text = _truncate_text(body_text, MAX_ARTICLE_TEXT_LENGTH)
        media = extract_article_media(
            soup,
            summary.article_url,
            body_container=body_container,
            fallback_cover_url=summary.cover_image_url,
        )

        return ParsedArticle(
            title=title,
            canonical_url=canonical_url,
            article_url=summary.article_url,
            raw_date=raw_date,
            date_info=date_info,
            body_text=body_text,
            raw_excerpt=summary.raw_excerpt,
            media_url=media.media_url,
            media_urls=media.media_urls,
            media_type=media.media_type,
        )

    def from_summary(self, summary: NewsArticleSummary) -> ParsedArticle:
        date_info = parse_article_date(summary.raw_date, self.article_timezone)
        return ParsedArticle(
            title=summary.title,
            canonical_url=summary.canonical_url,
            article_url=summary.article_url,
            raw_date=summary.raw_date,
            date_info=date_info,
            body_text=summary.raw_excerpt or "",
            raw_excerpt=summary.raw_excerpt,
            media_url=summary.cover_image_url,
            media_urls=(summary.cover_image_url,) if summary.cover_image_url else (),
            media_type="photo" if summary.cover_image_url else "none",
        )


def _select_body_container(soup: BeautifulSoup) -> Tag | None:
    for selector in (
        ".artText",
        ".article-content",
        ".news-content",
        ".content",
        "article",
        "main",
    ):
        container = soup.select_one(selector)
        if container is not None:
            return container

    return None


def _extract_title(soup: BeautifulSoup) -> str | None:
    title_node = soup.select_one("h1.artTitle, h1")
    if title_node is not None:
        title = _clean_text(title_node.get_text(" ", strip=True))
        if title:
            return title

    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        title = _meta_content(soup, selector)
        if title and not _looks_generic_title(title):
            return _clean_title(title)

    if soup.title and soup.title.string:
        return _clean_title(soup.title.string)

    return None


def _extract_article_date(soup: BeautifulSoup) -> str | None:
    time_node = soup.select_one("time[datetime]")
    if time_node is not None:
        value = time_node.get("datetime")
        if value:
            return _clean_text(str(value))

    for selector in (
        ".date",
        ".time",
        ".publish-time",
        ".pubdate",
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="publishdate"]',
        'meta[name="pubdate"]',
        'meta[name="date"]',
    ):
        if selector.startswith("meta"):
            value = _meta_content(soup, selector)
        else:
            node = soup.select_one(selector)
            value = node.get_text(" ", strip=True) if node else None
        if value:
            return _clean_text(value)

    return None


def _canonical_article_url(soup: BeautifulSoup, article_url: str) -> str:
    link = soup.select_one('link[rel="canonical"][href]')
    if link is not None:
        canonical = _normalize_article_url(str(link.get("href")), article_url)
        if canonical:
            return canonical

    for selector in ('meta[property="og:url"]', 'meta[name="og:url"]'):
        og_url = _meta_content(soup, selector)
        canonical = _normalize_article_url(og_url, article_url) if og_url else None
        if canonical and not _looks_like_homepage(canonical):
            return canonical

    normalized = _normalize_article_url(article_url, article_url)
    return normalized or article_url


def _extract_body_text(container: Tag) -> str:
    for node in container.select("script, style, noscript, iframe, nav, footer, header, .footer, .header-nav"):
        node.decompose()

    blocks: list[str] = []
    seen: set[str] = set()
    for node in container.find_all(["h2", "h3", "h4", "p", "li"]):
        text = _clean_text(node.get_text(" ", strip=True))
        if not text or _is_unrelated_text(text) or text in seen:
            continue

        seen.add(text)
        blocks.append(text)

    if blocks:
        return "\n".join(blocks)

    text = _clean_text(container.get_text(" ", strip=True))
    return "" if _is_unrelated_text(text) else text


def _is_unrelated_text(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    social_footer = "discord|x|facebook|instagram|tiktok|youtube|twitch"
    if normalized == social_footer:
        return True

    blocked = {
        "download",
        "log in",
        "my account log out",
        "privacy policy",
        "term of use",
        "support",
        "comming soon",
    }
    return text.strip().lower() in blocked


def _meta_content(soup: BeautifulSoup, selector: str) -> str | None:
    tag = soup.select_one(selector)
    if tag is None:
        return None

    content = tag.get("content")
    return _clean_text(str(content)) if content else None


def _normalize_article_url(value: str, base_url: str) -> str | None:
    absolute_url = urljoin(base_url, value.strip())
    absolute_url, _fragment = urldefrag(absolute_url)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return absolute_url


def _looks_like_homepage(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    return path in {"", "/index.html"}


def _looks_generic_title(value: str) -> bool:
    return "super hero team-based pvp shooter" in value.lower()


def _clean_title(value: str) -> str:
    title = _clean_text(value)
    for separator in ("_Marvel Rivals", " | Marvel Rivals", " - Marvel Rivals"):
        if separator in title:
            title = title.split(separator, 1)[0]
    return title.strip()


def _clean_text(value: object) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value

    truncated = value[:max_length].rsplit("\n", 1)[0].strip()
    return truncated or value[:max_length].strip()
