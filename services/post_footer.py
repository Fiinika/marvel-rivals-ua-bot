from __future__ import annotations

from html import escape

from services.i18n import t, t_optional


FOOTER_TITLE_KEY = "post_footer.title"
FOOTER_SEPARATOR_KEY = "post_footer.separator"


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
    separator = t(FOOTER_SEPARATOR_KEY)
    title = t(FOOTER_TITLE_KEY)
    links = [
        _format_plain_link("post_footer.links.chat"),
        _format_plain_link("post_footer.links.submission"),
        _format_plain_link("post_footer.links.discord"),
    ]
    links_line = t("post_footer.link_separator").join(link for link in links if link)

    parts = [separator, title]
    if links_line:
        parts.append(links_line)

    return "\n".join(part for part in parts if part).strip()


def format_community_footer_html() -> str:
    separator = escape(t(FOOTER_SEPARATOR_KEY))
    title = escape(t(FOOTER_TITLE_KEY))
    links = [
        _format_html_link("post_footer.links.chat"),
        _format_html_link("post_footer.links.submission"),
        _format_html_link("post_footer.links.discord"),
    ]
    links_line = escape(t("post_footer.link_separator")).join(link for link in links if link)

    parts = [separator, title]
    if links_line:
        parts.append(links_line)

    return "\n".join(part for part in parts if part).strip()


def format_post_html(text: str) -> str:
    body = strip_community_footer(text).strip()
    footer = format_community_footer_html()
    if not footer:
        return escape(body)

    if not body:
        return footer

    return f"{escape(body)}\n\n{footer}"


def strip_community_footer(text: str) -> str:
    separator = t(FOOTER_SEPARATOR_KEY)
    title = t(FOOTER_TITLE_KEY)

    for marker in (separator, title):
        index = text.find(marker)
        if index != -1:
            return text[:index].rstrip()

    return text


def _format_plain_link(key_prefix: str) -> str:
    label = t(f"{key_prefix}.label").strip()
    if not label:
        return ""

    return label


def _format_html_link(key_prefix: str) -> str:
    label = t(f"{key_prefix}.label").strip()
    url = t_optional(f"{key_prefix}.url", "").strip()
    if not label:
        return ""

    if not url:
        return escape(label)

    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
