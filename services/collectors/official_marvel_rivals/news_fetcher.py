from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; MarvelRivalsUACollector/1.0; "
    "+https://www.marvelrivals.com/news/)"
)
REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class NewsArticleSummary:
    title: str
    canonical_url: str
    article_url: str
    raw_date: str | None
    raw_excerpt: str | None
    cover_image_url: str | None


async def fetch_html(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


class OfficialNewsFetcher:
    def __init__(self, news_url: str) -> None:
        self.news_url = news_url

    async def fetch_recent_articles(self) -> list[NewsArticleSummary]:
        html = await fetch_html(self.news_url)
        if html is None:
            return []

        try:
            articles = parse_official_news_list(html, self.news_url)
        except Exception:
            logger.exception("Failed to parse official Marvel Rivals news list")
            return []

        if not articles:
            logger.warning("No articles found on official news page %s", self.news_url)
        else:
            logger.info("Found %s official Marvel Rivals news articles", len(articles))

        return articles


def parse_official_news_list(html: str, base_url: str) -> list[NewsArticleSummary]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[NewsArticleSummary] = []
    seen_urls: set[str] = set()

    for item in soup.select("a.list-item[href]"):
        article = _summary_from_card(item, base_url)
        if article is None or article.canonical_url in seen_urls:
            continue

        seen_urls.add(article.canonical_url)
        articles.append(article)

    if articles:
        return articles

    logger.warning("Official news list cards were not found. Falling back to link scanning.")
    for link in soup.select("a[href]"):
        article = _summary_from_link(link, base_url)
        if article is None or article.canonical_url in seen_urls:
            continue

        seen_urls.add(article.canonical_url)
        articles.append(article)

    return articles


def _summary_from_card(item: Tag, base_url: str) -> NewsArticleSummary | None:
    href = item.get("href")
    article_url = _normalize_article_url(str(href), base_url) if href else None
    if article_url is None:
        return None

    title = _clean_text(_first_text(item, "h1, h2, h3") or item.get("title") or "")
    if not title:
        title = _title_from_url(article_url)

    raw_excerpt = _clean_text(_first_text(item, "p") or "")
    raw_date = _clean_text(_first_text(item, "time, .date, .time") or "")
    cover_image_url = _normalize_media_url(_first_image_src(item), base_url)

    return NewsArticleSummary(
        title=title,
        canonical_url=article_url,
        article_url=article_url,
        raw_date=raw_date or None,
        raw_excerpt=raw_excerpt or None,
        cover_image_url=cover_image_url,
    )


def _summary_from_link(link: Tag, base_url: str) -> NewsArticleSummary | None:
    href = link.get("href")
    article_url = _normalize_article_url(str(href), base_url) if href else None
    if article_url is None:
        return None

    title = _clean_text(link.get_text(" ", strip=True) or str(link.get("title") or ""))
    if not title:
        title = _title_from_url(article_url)

    return NewsArticleSummary(
        title=title,
        canonical_url=article_url,
        article_url=article_url,
        raw_date=None,
        raw_excerpt=None,
        cover_image_url=_normalize_media_url(_first_image_src(link), base_url),
    )


def _normalize_article_url(value: str, base_url: str) -> str | None:
    absolute_url = urljoin(base_url, value.strip())
    absolute_url, _fragment = urldefrag(absolute_url)
    parsed = urlparse(absolute_url)

    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != "www.marvelrivals.com":
        return None
    if not parsed.path.endswith(".html"):
        return None

    return absolute_url


def _normalize_media_url(value: str | None, base_url: str) -> str | None:
    if not value:
        return None

    absolute_url = urljoin(base_url, value.strip())
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return absolute_url


def _first_text(item: Tag, selector: str) -> str | None:
    match = item.select_one(selector)
    return match.get_text(" ", strip=True) if match else None


def _first_image_src(item: Tag) -> str | None:
    image = item.select_one("img[src], img[data-src], img[data-original]")
    if image is None:
        return None

    value = image.get("src") or image.get("data-src") or image.get("data-original")
    return str(value).strip() if value else None


def _clean_text(value: object) -> str:
    return " ".join(str(value).split())


def _title_from_url(url: str) -> str:
    path = urlparse(url).path
    return path.rsplit("/", 1)[-1].replace(".html", "")
