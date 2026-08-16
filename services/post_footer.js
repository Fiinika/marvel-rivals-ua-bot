import { t, tOptional } from "./i18n.js";
import { htmlEscape, rstrip } from "./pyutils.js";
import { isSafeHttpUrl } from "./urlutils.js";

export const FOOTER_TITLE_KEY = "post_footer.title";
export const FOOTER_SEPARATOR_KEY = "post_footer.separator";
export const OFFICIAL_SOURCE_ATTRIBUTION = "Повні деталі — на офіційному сайті.";
const OFFICIAL_SOURCE_PREFIX = "Повні деталі — на ";
const OFFICIAL_SOURCE_LABEL = "офіційному сайті";
const OFFICIAL_SOURCE_SUFFIX = ".";
// Non-official sources attribute via a "Джерело: <name>" line (see gemini.source_line);
// the source name is turned into a link to the original post.
const GENERIC_SOURCE_PREFIX = "Джерело:";
// Footer link copy and URLs both live in locales/uk.json under post_footer.links.
export const FOOTER_LINK_KEYS = [
  "post_footer.links.chat",
  "post_footer.links.submission",
  "post_footer.links.discord",
];

// A private-use marker (U+E000) placed in front of the appended community footer
// so its position is found STRUCTURALLY, never by matching the visible title
// text. An untrusted post body could otherwise contain that title to suppress or
// hijack the footer. The sentinel is stripped from incoming text and from the
// final output, so it never reaches Telegram.
const FOOTER_SENTINEL = "";

export function formatCommunityFooter() {
  const separator = t(FOOTER_SEPARATOR_KEY);
  const title = t(FOOTER_TITLE_KEY);
  const links = FOOTER_LINK_KEYS.map((keyPrefix) => formatPlainLink(keyPrefix));
  const linksLine = links.filter(Boolean).join(t("post_footer.link_separator"));

  const parts = [separator, title];
  if (linksLine) {
    parts.push(linksLine);
  }

  return parts.filter(Boolean).join("\n").trim();
}

/**
 * The community footer as ready HTML (links applied) for callers that build
 * their own HTML body and bypass formatPostHtml — e.g. the album digest caption.
 */
export function formatCommunityFooterHtml() {
  return linkFooterItems(FOOTER_SENTINEL + htmlEscape(formatCommunityFooter()));
}

export function formatPostHtml(
  text,
  { source_url = null, allow_source_link = false, include_community_footer = false } = {},
) {
  const body = prepareBodyText(text, include_community_footer);
  const renderedBody = formatBodyHtml(body, {
    sourceUrl: source_url,
    allowSourceLink: allow_source_link,
    linkFooter: include_community_footer,
  });
  // Belt-and-suspenders: the sentinel is consumed by linkFooterItems, but make
  // absolutely sure it never reaches Telegram even on an unexpected path.
  return renderedBody.split(FOOTER_SENTINEL).join("");
}

/**
 * Whether a submission's attribution line may be rendered as a source link.
 *
 * True for any collector-sourced submission — one that carries both a
 * `source_type` and a `source_url`. User submissions (no source_type) and rows
 * without a URL keep plain-text attribution.
 */
export function submissionAllowsSourceLink(part) {
  return Boolean(String(part?.source_type ?? "").trim()) && Boolean(String(part?.source_url ?? "").trim());
}

/**
 * Remove the appended community-footer block, located by its structural
 * sentinel — never by matching the visible title, which an untrusted body could
 * contain. Text without the sentinel (i.e. anything we did not just footer) is
 * returned unchanged.
 */
export function stripCommunityFooter(text) {
  const index = String(text).indexOf(FOOTER_SENTINEL);
  if (index === -1) {
    return text;
  }
  return rstrip(String(text).slice(0, index));
}

function formatPlainLink(keyPrefix) {
  const label = t(`${keyPrefix}.label`).trim();
  if (!label) {
    return "";
  }

  return label;
}

function footerLinkUrl(keyPrefix) {
  return tOptional(`${keyPrefix}.url`, "").trim();
}

function prepareBodyText(text, includeCommunityFooter) {
  // Strip any sentinel an untrusted body might contain, so it cannot fake or move
  // the footer marker we add below (the suppression/hijack vector).
  const body = String(text ?? "")
    .split(FOOTER_SENTINEL)
    .join("")
    .trim();
  if (!includeCommunityFooter) {
    return body;
  }

  const footer = formatCommunityFooter();
  if (!footer) {
    return body;
  }

  // The footer is ALWAYS appended fresh (the rendered output strips the sentinel
  // and is never re-fed, so there is nothing to double-append), and is marked with
  // the sentinel so linkFooterItems can find it structurally.
  const block = `${FOOTER_SENTINEL}${footer}`;
  return body ? `${body}\n\n${block}` : block;
}

function formatBodyHtml(text, { sourceUrl, allowSourceLink, linkFooter }) {
  let rendered;
  if (!allowSourceLink || !sourceUrl || !isSafeHttpUrl(sourceUrl)) {
    rendered = htmlEscape(text);
  } else if (text.includes(OFFICIAL_SOURCE_ATTRIBUTION)) {
    const sourceLink =
      `${htmlEscape(OFFICIAL_SOURCE_PREFIX)}` +
      `<a href="${htmlEscape(sourceUrl, true)}">${htmlEscape(OFFICIAL_SOURCE_LABEL)}</a>` +
      `${htmlEscape(OFFICIAL_SOURCE_SUFFIX)}`;
    rendered = text
      .split(OFFICIAL_SOURCE_ATTRIBUTION)
      .map((part) => htmlEscape(part))
      .join(sourceLink);
  } else {
    rendered = linkifyGenericSource(text, sourceUrl);
  }

  if (linkFooter) {
    rendered = linkFooterItems(rendered);
  }

  return rendered;
}

/**
 * Turn a "Джерело: <name>" line into a link to the source, escaping the rest.
 *
 * Falls back to plain escaped text when no such line is present, so non-source
 * posts are unaffected.
 */
function linkifyGenericSource(text, sourceUrl) {
  const lines = text.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const stripped = line.trim();
    if (!stripped.startsWith(GENERIC_SOURCE_PREFIX)) {
      continue;
    }

    const name = rstrip(stripped.slice(GENERIC_SOURCE_PREFIX.length).trim(), ".").trim();
    if (!name) {
      continue;
    }

    const leading = line.slice(0, line.length - line.replace(/^\s+/, "").length);
    const linked =
      `${htmlEscape(leading)}${htmlEscape(GENERIC_SOURCE_PREFIX)} ` +
      `<a href="${htmlEscape(sourceUrl, true)}">${htmlEscape(name)}</a>`;
    const renderedLines = lines.map((other) => htmlEscape(other));
    renderedLines[index] = linked;
    return renderedLines.join("\n");
  }

  return htmlEscape(text);
}

function splitFooterLabel(label) {
  const index = label.indexOf(" ");
  if (index === -1) {
    return ["", label];
  }

  return [label.slice(0, index), label.slice(index + 1).trim()];
}

function linkFooterItems(renderedHtml) {
  // Locate the footer by the structural sentinel (placed just before it) rather
  // than the visible title, and drop the sentinel from the output.
  const markerIndex = renderedHtml.indexOf(FOOTER_SENTINEL);
  if (markerIndex === -1) {
    return renderedHtml;
  }

  const beforeFooter = renderedHtml.slice(0, markerIndex);
  let footer = renderedHtml.slice(markerIndex + FOOTER_SENTINEL.length);
  for (const keyPrefix of FOOTER_LINK_KEYS) {
    const url = footerLinkUrl(keyPrefix);
    if (!url || !isSafeHttpUrl(url)) {
      continue;
    }

    const label = t(`${keyPrefix}.label`).trim();
    const [icon, linkLabel] = splitFooterLabel(label);
    if (!linkLabel) {
      continue;
    }

    const plain = htmlEscape(label);
    const linked = `${htmlEscape(icon)} <a href="${htmlEscape(url, true)}">${htmlEscape(linkLabel)}</a>`;
    footer = replaceOnce(footer, plain, linked);
  }

  return beforeFooter + footer;
}

/** Python's `str.replace(old, new, 1)` — first occurrence only. */
function replaceOnce(text, search, replacement) {
  const index = text.indexOf(search);
  if (index === -1) {
    return text;
  }
  return text.slice(0, index) + replacement + text.slice(index + search.length);
}
