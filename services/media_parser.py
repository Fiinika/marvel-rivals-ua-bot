from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag


logger = logging.getLogger(__name__)

PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_ARTICLE_MEDIA_ITEMS = 4


@dataclass(frozen=True)
class ArticleMedia:
    media_urls: tuple[str, ...]
    media_type: str

    @property
    def media_url(self) -> str | None:
        return self.media_urls[0] if self.media_urls else None


def extract_article_media(
    soup: BeautifulSoup,
    page_url: str,
    *,
    body_container: Tag | None = None,
    fallback_cover_url: str | None = None,
) -> ArticleMedia:
    candidates: list[tuple[str, str]] = []

    for selector in (
        'meta[property="og:image"]',
        'meta[name="og:image"]',
        'meta[property="twitter:image"]',
        'meta[name="twitter:image"]',
    ):
        value = _meta_content(soup, selector)
        if value:
            candidates.append((selector, value))

    if fallback_cover_url:
        candidates.append(("list cover image", fallback_cover_url))

    image_scope = body_container or soup
    for image in image_scope.select("img[src], img[data-src], img[data-original]"):
        src = image.get("src") or image.get("data-src") or image.get("data-original")
        if src:
            candidates.append(("article image", str(src)))

    selected_urls: list[str] = []
    seen_urls: set[str] = set()
    for source, candidate in candidates:
        media_url = _normalize_url(candidate, page_url)
        if not media_url or media_url in seen_urls:
            continue
        seen_urls.add(media_url)

        if _is_meaningful_photo_url(media_url):
            logger.info("Selected article media from %s: %s", source, media_url)
            selected_urls.append(media_url)
            if len(selected_urls) >= MAX_ARTICLE_MEDIA_ITEMS:
                break
            continue

        logger.debug("Skipped non-article media candidate from %s: %s", source, media_url)

    if selected_urls:
        return ArticleMedia(media_urls=tuple(selected_urls), media_type="photo")

    logger.info("No safe article media found for %s", page_url)
    return ArticleMedia(media_urls=(), media_type="none")


def _meta_content(soup: BeautifulSoup, selector: str) -> str | None:
    tag = soup.select_one(selector)
    if tag is None:
        return None

    content = tag.get("content")
    return str(content).strip() if content else None


def _normalize_url(value: str, base_url: str) -> str | None:
    value = value.strip()
    if not value:
        return None

    url = urljoin(base_url, value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return url


def _is_meaningful_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    filename = path.rsplit("/", 1)[-1]

    if not path.endswith(PHOTO_EXTENSIONS):
        return False

    blocked_tokens = (
        "avatar",
        "badge",
        "blank",
        "button",
        "captcha",
        "favicon",
        "icon",
        "lan_",
        "logo",
        "pixel",
        "sprite",
        "tracking",
    )
    if any(token in path for token in blocked_tokens):
        return False

    # The current official pages include a generic site share image in metadata.
    # Prefer article/list imagery over that global card.
    if filename == "share.jpg" or path.endswith("/data/share.jpg"):
        return False

    return True
