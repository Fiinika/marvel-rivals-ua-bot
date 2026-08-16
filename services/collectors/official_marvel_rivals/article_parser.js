import { parseArticleDate } from "../../date_utils.js";
import { getText, loadHtml, selectOne } from "../../html.js";
import { getLogger } from "../../logger.js";
import { extractArticleMedia } from "../../media_parser.js";
import { charLength, collapseWhitespace, rsplitOnce, sliceChars } from "../../pyutils.js";
import { urldefrag, urljoin, urlsplit } from "../../urlutils.js";
import { fetchHtml } from "./news_fetcher.js";

const logger = getLogger("services.collectors.official_marvel_rivals.article_parser");

export const MAX_ARTICLE_TEXT_LENGTH = 12000;

/**
 * @typedef {{title: string, canonical_url: string, article_url: string,
 *            raw_date: string|null, date_info: object|null, body_text: string,
 *            raw_excerpt: string|null, media_url: string|null,
 *            media_urls: string[], media_type: string}} ParsedArticle
 */

export class ArticleParser {
  constructor(articleTimezone) {
    this.article_timezone = articleTimezone;
  }

  async fetchAndParse(summary) {
    const html = await fetchHtml(summary.article_url);
    if (html === null) {
      logger.warning(`Using news-list fallback data for article ${summary.article_url}`);
      return this.fromSummary(summary);
    }

    try {
      return this.parse({ html, summary });
    } catch (error) {
      logger.exception(`Failed to parse article page ${summary.article_url}`, error);
      return this.fromSummary(summary);
    }
  }

  parse({ html, summary }) {
    const $ = loadHtml(html);
    const bodyContainer = selectBodyContainer($);
    const rawDate = extractArticleDate($) ?? summary.raw_date;
    const dateInfo = parseArticleDate(rawDate, this.article_timezone);
    const canonicalUrl = canonicalArticleUrl($, summary.article_url);
    const title = extractTitle($) ?? summary.title;
    let bodyText = extractBodyText($, bodyContainer ?? $.root());
    if (!bodyText && summary.raw_excerpt) {
      bodyText = summary.raw_excerpt;
    }

    bodyText = truncateText(bodyText, MAX_ARTICLE_TEXT_LENGTH);
    const media = extractArticleMedia($, summary.article_url, {
      bodyContainer,
      fallbackCoverUrl: summary.cover_image_url,
    });

    return {
      title,
      canonical_url: canonicalUrl,
      article_url: summary.article_url,
      raw_date: rawDate,
      date_info: dateInfo,
      body_text: bodyText,
      raw_excerpt: summary.raw_excerpt,
      media_url: media.media_url,
      media_urls: media.media_urls,
      media_type: media.media_type,
    };
  }

  fromSummary(summary) {
    const dateInfo = parseArticleDate(summary.raw_date, this.article_timezone);
    return {
      title: summary.title,
      canonical_url: summary.canonical_url,
      article_url: summary.article_url,
      raw_date: summary.raw_date,
      date_info: dateInfo,
      body_text: summary.raw_excerpt || "",
      raw_excerpt: summary.raw_excerpt,
      media_url: summary.cover_image_url,
      media_urls: summary.cover_image_url ? [summary.cover_image_url] : [],
      media_type: summary.cover_image_url ? "photo" : "none",
    };
  }
}

function selectBodyContainer($) {
  for (const selector of [".artText", ".article-content", ".news-content", ".content", "article", "main"]) {
    const container = selectOne($, selector);
    if (container !== null) {
      return container;
    }
  }

  return null;
}

function extractTitle($) {
  const titleNode = selectOne($, "h1.artTitle, h1");
  if (titleNode !== null) {
    const title = cleanText(getText(titleNode));
    if (title) {
      return title;
    }
  }

  for (const selector of ['meta[property="og:title"]', 'meta[name="twitter:title"]']) {
    const title = metaContent($, selector);
    if (title && !looksGenericTitle(title)) {
      return cleanTitle(title);
    }
  }

  const documentTitle = selectOne($, "title");
  if (documentTitle !== null && getText(documentTitle)) {
    return cleanTitle(getText(documentTitle));
  }

  return null;
}

function extractArticleDate($) {
  const timeNode = selectOne($, "time[datetime]");
  if (timeNode !== null) {
    const value = timeNode.attr("datetime");
    if (value) {
      return cleanText(String(value));
    }
  }

  for (const selector of [
    ".date",
    ".time",
    ".publish-time",
    ".pubdate",
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[name="publishdate"]',
    'meta[name="pubdate"]',
    'meta[name="date"]',
  ]) {
    let value;
    if (selector.startsWith("meta")) {
      value = metaContent($, selector);
    } else {
      const node = selectOne($, selector);
      value = node ? getText(node) : null;
    }
    if (value) {
      return cleanText(value);
    }
  }

  return null;
}

function canonicalArticleUrl($, articleUrl) {
  const link = selectOne($, 'link[rel="canonical"][href]');
  if (link !== null) {
    const canonical = normalizeArticleUrl(String(link.attr("href")), articleUrl);
    if (canonical) {
      return canonical;
    }
  }

  for (const selector of ['meta[property="og:url"]', 'meta[name="og:url"]']) {
    const ogUrl = metaContent($, selector);
    const canonical = ogUrl ? normalizeArticleUrl(ogUrl, articleUrl) : null;
    if (canonical && !looksLikeHomepage(canonical)) {
      return canonical;
    }
  }

  const normalized = normalizeArticleUrl(articleUrl, articleUrl);
  return normalized || articleUrl;
}

function extractBodyText($, container) {
  container.find("script, style, noscript, iframe, nav, footer, header, .footer, .header-nav").remove();

  const blocks = [];
  const seen = new Set();
  container.find("h2, h3, h4, p, li").each((_index, element) => {
    const text = cleanText(getText($(element)));
    if (!text || isUnrelatedText(text) || seen.has(text)) {
      return;
    }

    seen.add(text);
    blocks.push(text);
  });

  if (blocks.length) {
    return blocks.join("\n");
  }

  const text = cleanText(getText(container));
  return isUnrelatedText(text) ? "" : text;
}

function isUnrelatedText(text) {
  const normalized = text.toLowerCase().replaceAll(" ", "");
  const socialFooter = "discord|x|facebook|instagram|tiktok|youtube|twitch";
  if (normalized === socialFooter) {
    return true;
  }

  const blocked = new Set([
    "download",
    "log in",
    "my account log out",
    "privacy policy",
    "term of use",
    "support",
    "comming soon",
  ]);
  return blocked.has(text.trim().toLowerCase());
}

function metaContent($, selector) {
  const tag = selectOne($, selector);
  if (tag === null) {
    return null;
  }

  const content = tag.attr("content");
  return content ? cleanText(String(content)) : null;
}

function normalizeArticleUrl(value, baseUrl) {
  const absoluteUrl = urldefrag(urljoin(baseUrl, value.trim()));
  const parsed = urlsplit(absoluteUrl);
  if ((parsed.scheme !== "http" && parsed.scheme !== "https") || !parsed.netloc) {
    return null;
  }

  return absoluteUrl;
}

function looksLikeHomepage(url) {
  const path = urlsplit(url).path.replace(/\/+$/, "");
  return path === "" || path === "/index.html";
}

function looksGenericTitle(value) {
  return value.toLowerCase().includes("super hero team-based pvp shooter");
}

function cleanTitle(value) {
  let title = cleanText(value);
  for (const separator of ["_Marvel Rivals", " | Marvel Rivals", " - Marvel Rivals"]) {
    if (title.includes(separator)) {
      title = title.split(separator)[0];
    }
  }
  return title.trim();
}

function cleanText(value) {
  return collapseWhitespace(String(value).replaceAll(" ", " "));
}

function truncateText(value, maxLength) {
  if (charLength(value) <= maxLength) {
    return value;
  }

  const head = sliceChars(value, 0, maxLength);
  const parts = rsplitOnce(head, "\n");
  const truncated = (parts.length === 2 ? parts[0] : parts[0]).trim();
  return truncated || head.trim();
}
