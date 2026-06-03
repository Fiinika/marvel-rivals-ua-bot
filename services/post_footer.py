from __future__ import annotations

import os
from html import escape
from urllib.parse import urlparse

from services.i18n import t


FOOTER_TITLE_KEY = "post_footer.title"
FOOTER_SEPARATOR_KEY = "post_footer.separator"
ENABLE_COMMUNITY_FOOTER_ENV = "ENABLE_COMMUNITY_FOOTER"
COMMUNITY_CHAT_URL_ENV = "COMMUNITY_CHAT_URL"
SUBMISSION_BOT_URL_ENV = "SUBMISSION_BOT_URL"
DISCORD_URL_ENV = "DISCORD_URL"
OFFICIAL_SOURCE_ATTRIBUTION = "Повні деталі — на офіційному сайті."
OFFICIAL_SOURCE_PREFIX = "Повні деталі — на "
OFFICIAL_SOURCE_LABEL = "офіційному сайті"
OFFICIAL_SOURCE_SUFFIX = "."
FOOTER_LINK_SPECS = (
    ("post_footer.links.chat", COMMUNITY_CHAT_URL_ENV),
    ("post_footer.links.submission", SUBMISSION_BOT_URL_ENV),
    ("post_footer.links.discord", DISCORD_URL_ENV),
)


def append_community_footer(text: str) -> str:
    text = strip_community_footer(text).strip()
    footer = format_community_footer()
    if not footer:
        return text

    title = t(FOOTER_TITLE_KEY)
    if title in text:
        return text

    return f"{text}\n\n{footer}" if text else footer


def format_community_footer() -> str:
    if not _community_footer_enabled():
        return ""

    separator = t(FOOTER_SEPARATOR_KEY)
    title = t(FOOTER_TITLE_KEY)
    links = [_format_plain_link(key_prefix) for key_prefix, _env_name in FOOTER_LINK_SPECS]
    links_line = t("post_footer.link_separator").join(link for link in links if link)

    parts = [separator, title]
    if links_line:
        parts.append(links_line)

    return "\n".join(part for part in parts if part).strip()


def format_community_footer_html() -> str:
    if not _community_footer_enabled():
        return ""

    separator = escape(t(FOOTER_SEPARATOR_KEY))
    title = escape(t(FOOTER_TITLE_KEY))
    links = [_format_html_link(key_prefix, env_name) for key_prefix, env_name in FOOTER_LINK_SPECS]
    links_line = escape(t("post_footer.link_separator")).join(link for link in links if link)

    parts = [separator, title]
    if links_line:
        parts.append(links_line)

    return "\n".join(part for part in parts if part).strip()


def format_post_html(
    text: str,
    *,
    source_url: str | None = None,
    allow_source_link: bool = False,
    include_community_footer: bool = False,
) -> str:
    body = _prepare_body_text(text, include_community_footer=include_community_footer)
    rendered_body = _format_body_html(
        body,
        source_url=source_url,
        allow_source_link=allow_source_link,
        link_footer=include_community_footer and _community_footer_enabled(),
    )
    return rendered_body


def strip_community_footer(text: str) -> str:
    separator = t(FOOTER_SEPARATOR_KEY)
    title = t(FOOTER_TITLE_KEY)
    title_index = text.find(title)
    if title_index == -1:
        return text

    prefix = text[:title_index].rstrip()
    if separator and prefix.endswith(separator):
        prefix = prefix[: -len(separator)].rstrip()

    return prefix


def _format_plain_link(key_prefix: str) -> str:
    label = t(f"{key_prefix}.label").strip()
    if not label:
        return ""

    return label


def _format_html_link(key_prefix: str, env_name: str) -> str:
    label = t(f"{key_prefix}.label").strip()
    url = os.getenv(env_name, "").strip()
    if not label:
        return ""

    if not url or not _is_safe_http_url(url):
        return escape(label)

    icon, link_label = _split_footer_label(label)
    if not link_label:
        return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'

    return f'{escape(icon)} <a href="{escape(url, quote=True)}">{escape(link_label)}</a>'


def _prepare_body_text(text: str, *, include_community_footer: bool) -> str:
    body = str(text or "").strip()
    if not include_community_footer:
        return strip_community_footer(body).strip()

    if not _community_footer_enabled():
        return strip_community_footer(body).strip()

    if _has_community_footer(body):
        return body

    footer = format_community_footer()
    if not footer:
        return body

    return f"{body}\n\n{footer}" if body else footer


def _format_body_html(
    text: str,
    *,
    source_url: str | None,
    allow_source_link: bool,
    link_footer: bool,
) -> str:
    if not allow_source_link or not source_url or not _is_safe_http_url(source_url):
        rendered = escape(text)
    elif OFFICIAL_SOURCE_ATTRIBUTION not in text:
        rendered = escape(text)
    else:
        source_link = (
            f"{escape(OFFICIAL_SOURCE_PREFIX)}"
            f'<a href="{escape(source_url, quote=True)}">{escape(OFFICIAL_SOURCE_LABEL)}</a>'
            f"{escape(OFFICIAL_SOURCE_SUFFIX)}"
        )
        rendered = source_link.join(escape(part) for part in text.split(OFFICIAL_SOURCE_ATTRIBUTION))

    if link_footer:
        rendered = _link_footer_items(rendered)

    return rendered


def _is_safe_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _community_footer_enabled() -> bool:
    value = os.getenv(ENABLE_COMMUNITY_FOOTER_ENV, "true").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _has_community_footer(text: str) -> bool:
    title = t(FOOTER_TITLE_KEY)
    return bool(title and title in text)


def _split_footer_label(label: str) -> tuple[str, str]:
    icon, separator, link_label = label.partition(" ")
    if not separator:
        return "", label

    return icon, link_label.strip()


def _link_footer_items(rendered_html: str) -> str:
    marker = escape(t(FOOTER_TITLE_KEY))
    marker_index = rendered_html.find(marker)
    if marker_index == -1:
        return rendered_html

    before_footer = rendered_html[:marker_index]
    footer = rendered_html[marker_index:]
    for key_prefix, env_name in FOOTER_LINK_SPECS:
        url = os.getenv(env_name, "").strip()
        if not url or not _is_safe_http_url(url):
            continue

        label = t(f"{key_prefix}.label").strip()
        icon, link_label = _split_footer_label(label)
        if not link_label:
            continue

        plain = escape(label)
        linked = f'{escape(icon)} <a href="{escape(url, quote=True)}">{escape(link_label)}</a>'
        footer = footer.replace(plain, linked, 1)

    return before_footer + footer
