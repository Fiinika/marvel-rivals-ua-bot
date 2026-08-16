import { getText, loadHtml, selectOne } from "../../html.js";
import { getLogger } from "../../logger.js";
import { collapseWhitespace, errorText, rsplitOnce } from "../../pyutils.js";
import { urldefrag, urljoin, urlsplit } from "../../urlutils.js";

const logger = getLogger("services.collectors.official_marvel_rivals.news_fetcher");

export const USER_AGENT =
  "Mozilla/5.0 (compatible; MarvelRivalsUACollector/1.0; +https://www.marvelrivals.com/news/)";
export const REQUEST_TIMEOUT_SECONDS = 20.0;

/**
 * @typedef {{title: string, canonical_url: string, article_url: string,
 *            raw_date: string|null, raw_excerpt: string|null,
 *            cover_image_url: string|null}} NewsArticleSummary
 */

export async function fetchHtml(url) {
  try {
    const response = await fetch(url, {
      redirect: "follow",
      headers: { "User-Agent": USER_AGENT },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_SECONDS * 1000),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await response.text();
  } catch (error) {
    logger.warning(`Failed to fetch ${url}: ${errorText(error)}`);
    return null;
  }
}

export class OfficialNewsFetcher {
  constructor(newsUrl) {
    this.news_url = newsUrl;
  }

  async fetchRecentArticles() {
    const html = await fetchHtml(this.news_url);
    if (html === null) {
      return [];
    }

    let articles;
    try {
      articles = parseOfficialNewsList(html, this.news_url);
    } catch (error) {
      logger.exception("Failed to parse official Marvel Rivals news list", error);
      return [];
    }

    if (!articles.length) {
      logger.warning(`No articles found on official news page ${this.news_url}`);
    } else {
      logger.info(`Found ${articles.length} official Marvel Rivals news articles`);
    }

    return articles;
  }
}

export function parseOfficialNewsList(html, baseUrl) {
  const $ = loadHtml(html);
  const articles = [];
  const seenUrls = new Set();

  $("a.list-item[href]").each((_index, element) => {
    const article = summaryFromCard($, $(element), baseUrl);
    if (article === null || seenUrls.has(article.canonical_url)) {
      return;
    }

    seenUrls.add(article.canonical_url);
    articles.push(article);
  });

  if (articles.length) {
    return articles;
  }

  logger.warning("Official news list cards were not found. Falling back to link scanning.");
  $("a[href]").each((_index, element) => {
    const article = summaryFromLink($, $(element), baseUrl);
    if (article === null || seenUrls.has(article.canonical_url)) {
      return;
    }

    seenUrls.add(article.canonical_url);
    articles.push(article);
  });

  return articles;
}

function summaryFromCard($, item, baseUrl) {
  const href = item.attr("href");
  const articleUrl = href ? normalizeArticleUrl(String(href), baseUrl) : null;
  if (articleUrl === null) {
    return null;
  }

  let title = cleanText(firstText($, item, "h1, h2, h3") ?? item.attr("title") ?? "");
  if (!title) {
    title = titleFromUrl(articleUrl);
  }

  const rawExcerpt = cleanText(firstText($, item, "p") ?? "");
  const rawDate = cleanText(firstText($, item, "time, .date, .time") ?? "");
  const coverImageUrl = normalizeMediaUrl(firstImageSrc($, item), baseUrl);

  return {
    title,
    canonical_url: articleUrl,
    article_url: articleUrl,
    raw_date: rawDate || null,
    raw_excerpt: rawExcerpt || null,
    cover_image_url: coverImageUrl,
  };
}

function summaryFromLink($, link, baseUrl) {
  const href = link.attr("href");
  const articleUrl = href ? normalizeArticleUrl(String(href), baseUrl) : null;
  if (articleUrl === null) {
    return null;
  }

  let title = cleanText(getText(link) || String(link.attr("title") ?? ""));
  if (!title) {
    title = titleFromUrl(articleUrl);
  }

  return {
    title,
    canonical_url: articleUrl,
    article_url: articleUrl,
    raw_date: null,
    raw_excerpt: null,
    cover_image_url: normalizeMediaUrl(firstImageSrc($, link), baseUrl),
  };
}

function normalizeArticleUrl(value, baseUrl) {
  const absoluteUrl = urldefrag(urljoin(baseUrl, value.trim()));
  const parsed = urlsplit(absoluteUrl);

  if (parsed.scheme !== "http" && parsed.scheme !== "https") {
    return null;
  }
  if (parsed.netloc.toLowerCase() !== "www.marvelrivals.com") {
    return null;
  }
  if (!parsed.path.endsWith(".html")) {
    return null;
  }

  return absoluteUrl;
}

function normalizeMediaUrl(value, baseUrl) {
  if (!value) {
    return null;
  }

  const absoluteUrl = urljoin(baseUrl, value.trim());
  const parsed = urlsplit(absoluteUrl);
  if ((parsed.scheme !== "http" && parsed.scheme !== "https") || !parsed.netloc) {
    return null;
  }

  return absoluteUrl;
}

function firstText($, item, selector) {
  const match = selectOne($, selector, item);
  return match ? getText(match) : null;
}

function firstImageSrc($, item) {
  const image = selectOne($, "img[src], img[data-src], img[data-original]", item);
  if (image === null) {
    return null;
  }

  const value = image.attr("src") || image.attr("data-src") || image.attr("data-original");
  return value ? String(value).trim() : null;
}

function cleanText(value) {
  return collapseWhitespace(String(value));
}

function titleFromUrl(url) {
  const path = urlsplit(url).path;
  const last = rsplitOnce(path, "/");
  return (last.length === 2 ? last[1] : last[0]).replaceAll(".html", "");
}
