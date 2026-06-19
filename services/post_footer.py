from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from services.i18n import t, t_optional


FOOTER_TITLE_KEY = "post_footer.title"
FOOTER_SEPARATOR_KEY = "post_footer.separator"
OFFICIAL_SOURCE_ATTRIBUTION = "Повні деталі — на офіційному сайті."
OFFICIAL_SOURCE_PREFIX = "Повні деталі — на "
OFFICIAL_SOURCE_LABEL = "офіційному сайті"
OFFICIAL_SOURCE_SUFFIX = "."
# Non-official sources attribute via a "Джерело: <name>" line (see gemini.source_line);
# the source name is turned into a link to the original post.
GENERIC_SOURCE_PREFIX = "Джерело:"
# Footer link copy and URLs both live in locales/uk.json under post_footer.links.
FOOTER_LINK_KEYS = (
    "post_footer.links.chat",
    "post_footer.links.submission",
    "post_footer.links.discord",
)


def format_community_footer() -> str:
    separator = t(FOOTER_SEPARATOR_KEY)
    title = t(FOOTER_TITLE_KEY)
    links = [_format_plain_link(key_prefix) for key_prefix in FOOTER_LINK_KEYS]
    links_line = t("post_footer.link_separator").join(link for link in links if link)

    parts = [separator, title]
    if links_line:
        parts.append(links_line)

    return "\n".join(part for part in parts if part).strip()


def format_community_footer_html() -> str:
    """The community footer as ready HTML (links applied) for callers that build
    their own HTML body and bypass format_post_html — e.g. the album digest caption."""
    return _link_footer_items(escape(format_community_footer()))


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
        link_footer=include_community_footer,
    )
    return rendered_body


def submission_allows_source_link(part: dict) -> bool:
    """Whether a submission's attribution line may be rendered as a source link.

    True for any collector-sourced submission — one that carries both a
    ``source_type`` and a ``source_url``. User submissions (no source_type) and
    rows without a URL keep plain-text attribution.
    """
    return bool(str(part.get("source_type") or "").strip()) and bool(str(part.get("source_url") or "").strip())


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


def _footer_link_url(key_prefix: str) -> str:
    return t_optional(f"{key_prefix}.url", "").strip()


def _prepare_body_text(text: str, *, include_community_footer: bool) -> str:
    body = str(text or "").strip()
    if not include_community_footer:
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
    elif OFFICIAL_SOURCE_ATTRIBUTION in text:
        source_link = (
            f"{escape(OFFICIAL_SOURCE_PREFIX)}"
            f'<a href="{escape(source_url, quote=True)}">{escape(OFFICIAL_SOURCE_LABEL)}</a>'
            f"{escape(OFFICIAL_SOURCE_SUFFIX)}"
        )
        rendered = source_link.join(escape(part) for part in text.split(OFFICIAL_SOURCE_ATTRIBUTION))
    else:
        rendered = _linkify_generic_source(text, source_url)

    if link_footer:
        rendered = _link_footer_items(rendered)

    return rendered


def _linkify_generic_source(text: str, source_url: str) -> str:
    """Turn a "Джерело: <name>" line into a link to the source, escaping the rest.

    Falls back to plain escaped text when no such line is present, so non-source
    posts are unaffected.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(GENERIC_SOURCE_PREFIX):
            continue

        name = stripped[len(GENERIC_SOURCE_PREFIX):].strip().rstrip(".").strip()
        if not name:
            continue

        leading = line[: len(line) - len(line.lstrip())]
        linked = (
            f"{escape(leading)}{escape(GENERIC_SOURCE_PREFIX)} "
            f'<a href="{escape(source_url, quote=True)}">{escape(name)}</a>'
        )
        rendered_lines = [escape(other) for other in lines]
        rendered_lines[index] = linked
        return "\n".join(rendered_lines)

    return escape(text)


def _is_safe_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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
    for key_prefix in FOOTER_LINK_KEYS:
        url = _footer_link_url(key_prefix)
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
