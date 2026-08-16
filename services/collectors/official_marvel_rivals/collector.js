import { buildUtcTimeConversionNotes } from "../../date_utils.js";
import { t } from "../../i18n.js";
import { collectorDefinition, draftCandidate, listingEntry } from "../base.js";
import { BaseNewsCollector } from "../runner.js";
import { ArticleParser } from "./article_parser.js";
import { OfficialNewsFetcher } from "./news_fetcher.js";

export const COLLECTOR_ID = "official_marvel_rivals";
export const SOURCE_TYPE = "official_marvel_rivals";

export const DEFINITION = collectorDefinition({
  collector_id: COLLECTOR_ID,
  source_type: SOURCE_TYPE,
  title_key: "collectors.official_marvel_rivals.title",
  button_key: "buttons.collector_official_marvel_rivals",
});

export class OfficialMarvelRivalsCollector extends BaseNewsCollector {
  static definition = DEFINITION;
  // The official site is the authoritative, full-detail source; never let it be
  // suppressed as a cross-source duplicate of a shorter social-media post.
  static participates_in_cross_source_dedup = false;

  constructor({ config, db, bot }) {
    super({ config, db, bot });
    this.fetcher = new OfficialNewsFetcher(config.official_news_url);
    this.parser = new ArticleParser(config.article_timezone);
  }

  missingGeminiWarning() {
    return t("collectors.official_marvel_rivals.errors.missing_gemini_api_key");
  }

  async fetchListing() {
    const summaries = await this.fetcher.fetchRecentArticles();
    return summaries.map((summary) => listingEntry(summary.canonical_url, summary));
  }

  async parseEntry(entry) {
    const summary = entry.payload;
    const article = await this.parser.fetchAndParse(summary);
    const sourceId = article.canonical_url || summary.canonical_url;
    const sourceUrl = article.canonical_url || article.article_url;
    const hasMedia = Boolean(article.media_url && article.media_type === "photo");
    const articleDate = article.date_info !== null ? article.date_info.article_date : null;
    const articleDateDisplay = article.date_info !== null ? article.date_info.article_date_display : null;

    return draftCandidate({
      source_id: sourceId,
      source_url: sourceUrl,
      title: article.title,
      body_text: article.body_text || article.raw_excerpt || "",
      source_name: t("collectors.official_marvel_rivals.source_name"),
      username: t("collectors.official_marvel_rivals.username"),
      original_text: buildOriginalText(article, sourceUrl, this.config.article_timezone),
      article_date: articleDate,
      article_date_display: articleDateDisplay,
      has_media: hasMedia,
      media_url: article.media_url,
      media_type: article.media_type,
      additional_media_urls: hasMedia ? article.media_urls.slice(1) : null,
    });
  }
}

function buildOriginalText(article, sourceUrl, articleTimezone) {
  const parts = [
    t("collectors.common.original_text.article_title", { value: article.title }),
    t("collectors.common.original_text.article_url", { value: sourceUrl }),
  ];

  if (article.raw_date) {
    parts.push(t("collectors.common.original_text.original_article_date", { value: article.raw_date }));
  }
  if (article.date_info !== null) {
    parts.push(
      t("collectors.common.original_text.converted_article_date", {
        value: article.date_info.article_date_display,
      }),
    );
  }

  const datetimeNotes = buildUtcTimeConversionNotes(article.body_text || article.raw_excerpt || "", {
    article_date: article.date_info !== null ? article.date_info.article_date : null,
    target_timezone: articleTimezone,
  });
  if (datetimeNotes) {
    parts.push("", "Конвертація UTC-часів для чернетки:", datetimeNotes);
  }

  if (article.body_text) {
    parts.push("", t("collectors.common.original_text.parsed_article_text"), article.body_text);
  } else if (article.raw_excerpt) {
    parts.push("", t("collectors.common.original_text.parsed_excerpt"), article.raw_excerpt);
  }

  if (article.media_urls.length) {
    const mediaLines = article.media_urls.map((mediaUrl) =>
      t("collectors.common.original_text.media_url", { value: mediaUrl }),
    );
    parts.push("", ...mediaLines, t("collectors.common.original_text.media_type", { value: article.media_type }));
  }

  return parts.join("\n").trim();
}
