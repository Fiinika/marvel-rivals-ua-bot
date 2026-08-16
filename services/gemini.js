import fs from "node:fs";
import path from "node:path";

import { t } from "./i18n.js";
import { getLogger } from "./logger.js";
import {
  charLength,
  errorText,
  escapeRegExp,
  formatTemplate,
  partition,
  rfindChars,
  rstrip,
  sliceChars,
  sleep,
  strip,
  WORD,
} from "./pyutils.js";

const logger = getLogger("services.gemini");

// Gemini's free tier throttles by requests-per-minute; a burst (e.g. several
// dedup + draft calls in one scheduler tick) trips a 429 that is transient. Retry
// it a few times with a bounded backoff so one throttle doesn't drop a draft.
const GEMINI_MAX_ATTEMPTS = 3;
const GEMINI_MAX_RETRY_DELAY_SECONDS = 20.0;

const PROMPTS_DIR = path.resolve(import.meta.dirname, "..", "prompts");
export const PROMPT_PATH = path.join(PROMPTS_DIR, "gemini_news_uk.md");
export const SHORT_FORM_PROMPT_PATH = path.join(PROMPTS_DIR, "gemini_shortform_uk.md");
export const STYLE_PROMPT_PATH = path.join(PROMPTS_DIR, "official_news_style.md");
export const DEDUP_PROMPT_PATH = path.join(PROMPTS_DIR, "gemini_dedup_uk.md");
export const WIKI_FACT_PROMPT_PATH = path.join(PROMPTS_DIR, "gemini_wiki_fact_uk.md");
export const WIKI_PICK_PROMPT_PATH = path.join(PROMPTS_DIR, "gemini_wiki_pick_uk.md");

export const POST_SEPARATOR = "---POST---";
export const TAGS_SEPARATOR = "---TAGS---";
export const OFFICIAL_SOURCE_TYPES = new Set(["official_marvel_rivals"]);
// Sources whose items are unofficial leaks/datamines: the short-form draft must
// frame them as rumours (чутки), never as confirmed/official statements.
export const RUMOR_SOURCE_TYPES = new Set(["reddit", "rivalskins"]);
// Sources that get the dedicated "Чи знали ви?" trivia prompt (translate + frame a
// wiki fact) instead of the social short-form prompt.
export const WIKI_FACT_SOURCE_TYPES = new Set(["wiki_facts"]);
export const OFFICIAL_BASE_HASHTAGS = ["#MarvelRivalsUA", "#Офіційно"];
export const OFFICIAL_SOURCE_ATTRIBUTION = "Повні деталі — на офіційному сайті.";
const OFFICIAL_TOPIC_TAG_RULES = [
  [["patch notes", "version update", "game update", "patch"], "#Патч"],
  [["fix", "bug", "issue", "known issue", "optimization", "optimisation"], "#Фікси"],
  [["balance", "hero changes", "ability changes"], "#Баланс"],
  [["event", "challenge", "reward", "token", "login bonus", "chrono"], "#Івент"],
  [["shop", "store", "units", "price", "limited-time", "limited time"], "#Магазин"],
  [["skin", "costume", "bundle", "cosmetic", "cosmetics"], "#Скіни"],
  [["trailer", "teaser", "video"], "#Трейлер"],
  [["hero", "character", "abilities", "ability"], "#Герої"],
  [["map"], "#Карта"],
  [["mode", "gameplay"], "#Геймплей"],
  [["season", "battle pass"], "#Сезон"],
  [["maintenance", "server", "downtime"], "#ТехнічніРоботи"],
  [["ranked", "competitive", "rank"], "#Рейтинг"],
  [["vote", "voting", "poll", "winner"], "#Голосування"],
  [["esports", "tournament"], "#Кіберспорт"],
  [["announcement", "notice"], "#Анонс"],
];
const OFFICIAL_POST_TYPE_RULES = [
  ["patch", "Patch notes / game update", ["patch notes", "version update", "game update", "patch"]],
  ["vote", "Vote / poll / community choice", ["vote", "voting", "poll", "winner", "community choice", "player choice"]],
  [
    "trailer",
    "Trailer / teaser / map reveal",
    ["trailer", "teaser", "video", "map reveal", "character reveal", "reveal"],
  ],
  ["event", "Event / rewards / login bonus", ["event", "challenge", "reward", "token", "login bonus", "chrono"]],
  [
    "shop",
    "Shop / skins / bundles",
    ["shop", "store", "skin", "costume", "bundle", "cosmetic", "cosmetics", "units", "price"],
  ],
];
const OFFICIAL_NORMAL_MAX_LENGTH = 1200;
const OFFICIAL_PATCH_MAX_LENGTH = 1600;
const OFFICIAL_SOURCE_LINK_PATTERN = /<a\s+[^>]*href=["'][^"']+["'][^>]*>\s*офіційному сайті\s*<\/a>/gi;

export class GeminiDraftError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "GeminiDraftError";
  }
}

/**
 * Normalised input for one draft.
 *
 * `extra_links` are (label, url) pairs lifted from the ORIGINAL post and already
 * validated by services/source_links. They are appended after the draft rather
 * than fed to the model, so a published link is never one the model wrote.
 */
export function geminiDraftInput({
  title,
  article_url,
  article_date_display = null,
  datetime_notes = null,
  body_text,
  source_type,
  source_name,
  extra_links = [],
}) {
  return Object.freeze({
    title,
    article_url,
    article_date_display,
    datetime_notes,
    body_text,
    source_type,
    source_name,
    extra_links,
  });
}

export class GeminiDraftGenerator {
  constructor(apiKey, model) {
    this.apiKey = apiKey;
    this.model = model;
  }

  async generateDrafts(draftInput, { maxPartLength }) {
    const pkg = await this.generateDraftPackage(draftInput, { maxPartLength });
    return pkg.draft_parts;
  }

  async generateDraftPackage(draftInput, { maxPartLength }) {
    const prompt = buildPrompt(draftInput);
    let draft;
    try {
      draft = await this.generateOnce(prompt);
    } catch (error) {
      if (error instanceof GeminiDraftError) {
        throw error;
      }
      if (isRateLimitError(error)) {
        throw new GeminiDraftError(
          "Gemini вичерпав ліміт запитів (rate limit). Спробуйте ще раз за хвилину.",
          { cause: error },
        );
      }
      throw new GeminiDraftError("Gemini draft generation failed", { cause: error });
    }

    draft = cleanResponseText(draft);
    if (!draft) {
      throw new GeminiDraftError("Gemini returned an empty draft");
    }

    const [draftText, extractedTags] = extractTags(draft);
    if (!draftText) {
      throw new GeminiDraftError("Gemini returned tags without draft text");
    }

    let tags;
    if (isOfficialSource(draftInput)) {
      tags = officialDatabaseTags(draftInput);
    } else {
      tags = extractedTags.length ? extractedTags : fallbackTags(draftInput);
    }

    let draftParts;
    if (isOfficialSource(draftInput)) {
      draftParts = [prepareOfficialDraft(draftText, draftInput, { maxPartLength })];
    } else {
      draftParts = splitDraftParts(draftText, { maxPartLength }).map((part) =>
        ensureRequiredMetadata(part, draftInput),
      );
    }
    return { draft_parts: draftParts, tags };
  }

  async generateDraft(draftInput) {
    const drafts = await this.generateDrafts(draftInput, { maxPartLength: 3500 });
    return drafts[0];
  }

  /**
   * Ask Gemini whether `newTitle` is the same specific story as any of
   * `existingTitles`. Returns a not-duplicate verdict when there is nothing
   * meaningful to compare; throws GeminiDraftError if the model call fails.
   */
  async findDuplicateTitle(newTitle, existingTitles) {
    const candidate = String(newTitle ?? "").trim();
    const known = existingTitles.filter((title) => title && title.trim()).map((title) => title.trim());
    if (!candidate || !known.length) {
      return { is_duplicate: false, matched_title: null };
    }

    const prompt = buildDedupPrompt(candidate, known);
    const raw = await this.generateOnce(prompt);
    return parseDuplicateVerdict(raw, known);
  }

  /**
   * Choose which shortlisted wiki fact makes the best "Чи знали ви?" post, as a
   * 0-based index into `facts`. Falls back to 0 — the caller's own top pick — on
   * anything unexpected, so a formatting quirk costs a slightly duller post
   * rather than the week's post.
   */
  async pickBestTriviaFact(facts) {
    const options = facts.filter((fact) => fact && fact.trim());
    if (options.length <= 1) {
      return 0;
    }
    const numbered = options.map((fact, index) => `${index + 1}. ${fact.trim()}`).join("\n");
    const prompt = formatTemplate(loadWikiPickPromptTemplate(), { facts: numbered });
    return parseTriviaPick(await this.generateOnce(prompt), options.length);
  }

  /**
   * One model call, with the same bounded retry the Python version performed
   * inside its worker thread.
   */
  async generateOnce(prompt) {
    let client;
    try {
      const { GoogleGenAI } = await import("@google/genai");
      client = new GoogleGenAI({ apiKey: this.apiKey });
    } catch (error) {
      throw new GeminiDraftError("@google/genai is not installed", { cause: error });
    }

    for (let attempt = 0; attempt < GEMINI_MAX_ATTEMPTS; attempt += 1) {
      try {
        const response = await client.models.generateContent({ model: this.model, contents: prompt });
        return response?.text ?? "";
      } catch (error) {
        if (attempt >= GEMINI_MAX_ATTEMPTS - 1 || !isRetryableGeminiError(error)) {
          throw error;
        }
        const delay = geminiRetryDelaySeconds(error, attempt);
        logger.warning(
          `Gemini call throttled/unavailable (${error?.status ?? "?"}); ` +
            `retrying in ${delay.toFixed(0)}s (attempt ${attempt + 1}/${GEMINI_MAX_ATTEMPTS})`,
        );
        await sleep(delay);
      }
    }
    // Unreachable: the final attempt either returns or re-throws above.
    throw new GeminiDraftError("Gemini call exhausted all retries");
  }
}

function isRateLimitError(error) {
  const code = error?.status;
  return code === 429 || errorText(error).includes("RESOURCE_EXHAUSTED");
}

/**
 * Retry per-minute rate limits (429) and transient server errors (500/503), but
 * NOT a per-DAY free-tier cap — that won't clear on a short retry, so failing
 * fast gives a clear message instead of stalling for nothing.
 */
function isRetryableGeminiError(error) {
  const text = errorText(error);
  if (text.includes("PerDay") || text.includes("RequestsPerDay")) {
    return false;
  }
  const code = error?.status;
  if (code === 429 || code === 500 || code === 503) {
    return true;
  }
  return ["RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL"].some((marker) => text.includes(marker));
}

/**
 * Honour the API's suggested retryDelay when present (capped), else use a
 * bounded exponential backoff.
 */
function geminiRetryDelaySeconds(error, attempt) {
  const match = /retryDelay['"]?\s*:\s*['"]?(\d+(?:\.\d+)?)/.exec(errorText(error));
  if (match) {
    return Math.min(Number(match[1]) + 1.0, GEMINI_MAX_RETRY_DELAY_SECONDS);
  }
  return Math.min(8.0 * (attempt + 1), GEMINI_MAX_RETRY_DELAY_SECONDS);
}

function buildPrompt(draftInput) {
  const sourceLine = sourceAttributionLine(draftInput);
  return formatTemplate(selectPromptTemplate(draftInput), {
    style_prompt: loadStylePrompt(),
    source_type: draftInput.source_type,
    source_name: draftInput.source_name,
    title: draftInput.title,
    article_url: draftInput.article_url,
    article_type_label: officialPostTypeLabel(draftInput),
    date_line: draftInput.article_date_display || t("gemini.fallback_date"),
    source_line: sourceLine,
    hashtag_line: publicHashtagLine(draftInput),
    datetime_notes: draftInput.datetime_notes || "UTC/GMT-часів для конвертації не знайдено.",
    body_text: draftInput.body_text || t("gemini.fallback_body"),
    rumor_notice: rumorNotice(draftInput),
  });
}

function isRumorSource(draftInput) {
  return RUMOR_SOURCE_TYPES.has(draftInput.source_type);
}

/**
 * A strong, source-scoped instruction for leak/datamine sources so the draft is
 * framed as a rumour. Empty for every other source (the placeholder then renders
 * as nothing).
 */
function rumorNotice(draftInput) {
  if (!isRumorSource(draftInput)) {
    return "";
  }

  return (
    "УВАГА: це НЕОФІЦІЙНИЙ злив/витік (датамайн або чутка), НЕ підтверджений розробниками. " +
    "Обов'язково познач це в тексті як чутку/неофіційну інформацію (напр. «за чутками», " +
    "«за даними датамайну», «неофіційно») і коротко нагадай, що деталі ще можуть змінитися. " +
    "Не подавай це як офіційну заяву чи підтверджений факт."
  );
}

/**
 * Wiki trivia uses the "Чи знали ви?" prompt; official articles use the
 * long-form article prompt; every other (social / short-form) source uses the
 * concise short-form prompt. All accept the same format placeholders, so the
 * values in buildPrompt stay shared.
 */
function selectPromptTemplate(draftInput) {
  if (WIKI_FACT_SOURCE_TYPES.has(draftInput.source_type)) {
    return loadWikiFactPromptTemplate();
  }
  if (isOfficialSource(draftInput)) {
    return loadPromptTemplate();
  }

  return loadShortFormPromptTemplate();
}

function cleanResponseText(value) {
  return strip(strip(strip(String(value ?? "")), "`"));
}

function buildDedupPrompt(newTitle, existingTitles) {
  const numbered = existingTitles.map((title, index) => `${index + 1}. ${title}`).join("\n");
  return formatTemplate(loadDedupPromptTemplate(), { new_title: newTitle, existing_titles: numbered });
}

/**
 * Interpret the model's `duplicate` field, biased toward not-a-duplicate.
 *
 * A genuine JSON `true`/`1` counts as a duplicate; everything else — including
 * the common stringified `"false"`/`"no"`/`"0"` deviations — is treated as NOT a
 * duplicate, so a formatting quirk never drops real news.
 */
function coerceDuplicateFlag(value) {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    return value === 1;
  }
  if (typeof value === "string") {
    return ["true", "yes", "1"].includes(value.trim().toLowerCase());
  }
  return false;
}

/**
 * Read the model's 1-based `pick` back as a 0-based index, failing open to the
 * first option on anything unexpected or out of range.
 */
export function parseTriviaPick(raw, optionCount) {
  const match = /\{[\s\S]*\}/.exec(cleanResponseText(raw));
  if (!match) {
    return 0;
  }
  let data;
  try {
    data = JSON.parse(match[0]);
  } catch {
    return 0;
  }
  const pick = Number(data?.pick);
  if (!Number.isInteger(pick) || pick < 1 || pick > optionCount) {
    return 0;
  }
  return pick - 1;
}

/**
 * Parse the model's JSON verdict, failing open (not a duplicate) on anything
 * unexpected — a missed dedup only costs a rare double post, but a false
 * positive would silently drop real news.
 */
export function parseDuplicateVerdict(raw, knownTitles) {
  const text = cleanResponseText(raw);
  const match = /\{[\s\S]*\}/.exec(text);
  if (!match) {
    return { is_duplicate: false, matched_title: null };
  }

  let data;
  try {
    data = JSON.parse(match[0]);
  } catch {
    return { is_duplicate: false, matched_title: null };
  }

  if (data === null || typeof data !== "object" || Array.isArray(data) || !coerceDuplicateFlag(data.duplicate)) {
    return { is_duplicate: false, matched_title: null };
  }

  const matched = data.match;
  let matchedTitle = null;
  if (typeof matched === "string" && matched.trim()) {
    const lowered = matched.trim().toLowerCase();
    matchedTitle = knownTitles.find((title) => title.toLowerCase() === lowered) ?? matched.trim();
  }

  return { is_duplicate: true, matched_title: matchedTitle };
}

function ensureRequiredMetadata(draft, draftInput) {
  // Short-form (non-official) posts deliberately carry NO publication-date line:
  // the post itself is the announcement, and a bare "2026-06-12" mid-post reads
  // as noise. The article date stays available to admins via the original_text.
  let result = cleanModelDraft(draft, draftInput).trim();
  result = applyDatetimeNotes(result, draftInput.datetime_notes);
  result = appendExtraLinks(result, draftInput);

  const sourceLine = sourceAttributionLine(draftInput);
  if (sourceLine && !hasSourceAttribution(result)) {
    result = `${result}\n\n${sourceLine}`.trim();
  }

  const hashtags = publicHashtagLine(draftInput);
  if (!result.includes(hashtags)) {
    result = `${result}\n\n${hashtags}`;
  }

  return normalizeBlankLines(result);
}

/**
 * Re-attach the post's own validated links, which URL stripping removed.
 *
 * They are added AFTER cleaning, so the strip cannot take them out again, and
 * they are rendered as bare URLs: Telegram makes those clickable on its own, and
 * the collector paths that carry links publish with previews disabled, so no
 * preview card appears.
 */
function appendExtraLinks(text, draftInput) {
  if (!draftInput.extra_links.length) {
    return text;
  }

  const lines = draftInput.extra_links
    .filter(([, url]) => !text.includes(url))
    .map(([label, url]) => `🔗 ${label}: ${url}`);
  if (!lines.length) {
    return text;
  }

  return text ? `${text}\n\n${lines.join("\n")}` : lines.join("\n");
}

function cleanModelDraft(draft, draftInput) {
  const lines = [];
  for (const line of draft.split("\n")) {
    const stripped = line.trim();
    if (!stripped) {
      lines.push(line);
      continue;
    }
    if (stripped === t("gemini.fallback_date")) {
      continue;
    }
    if (stripped.includes(draftInput.article_url)) {
      const cleanedLine = stripRawUrls(line).trim();
      if (!cleanedLine || looksLikeSourceOnlyLine(cleanedLine)) {
        continue;
      }
      lines.push(stripUnsupportedMarkdown(cleanedLine));
      continue;
    }
    if (/https?:\/\/|www\./i.test(stripped)) {
      const cleanedLine = stripRawUrls(line).trim();
      if (!cleanedLine || looksLikeSourceOnlyLine(cleanedLine)) {
        continue;
      }
      lines.push(stripUnsupportedMarkdown(cleanedLine));
      continue;
    }
    if (isHashtagOnlyLine(stripped)) {
      continue;
    }
    lines.push(stripUnsupportedMarkdown(line));
  }

  return stripRawUrls(lines.join("\n"));
}

function extractTags(draft) {
  if (!draft.includes(TAGS_SEPARATOR)) {
    return [draft, []];
  }

  const [draftText, , rawTags] = partition(draft, TAGS_SEPARATOR);
  return [draftText.trim(), normalizeTags(rawTags)];
}

function normalizeTags(rawTags) {
  const normalizedTags = [];
  const seen = new Set();
  for (const value of rawTags.split(/[,;\n]/)) {
    // lstrip("#"): the LEADING hashes only — a trailing "#" is punctuation the
    // final strip() below deals with.
    let normalized = value.trim().toLowerCase().replace(/^#+/, "");
    normalized = normalized.replace(/[_-]+/g, " ");
    normalized = normalized.replace(/\s+/g, " ");
    normalized = strip(normalized, ".,;:!?'\"()[]{}");
    if (!normalized || seen.has(normalized) || normalized.length > 40) {
      continue;
    }

    seen.add(normalized);
    normalizedTags.push(normalized);

    if (normalizedTags.length >= 12) {
      break;
    }
  }

  return normalizedTags;
}

function fallbackTags(draftInput) {
  const fallbackTag = t("gemini.fallback_tag");
  if (isWikiFactSource(draftInput)) {
    // Keep the stored tags in step with the rubric's public hashtags.
    return [fallbackTag, t("collectors.wiki_facts.tag")];
  }

  const tags = [fallbackTag];
  if (isRumorSource(draftInput)) {
    // Keep the stored tags in step with the permanent public "#Чутки" marker.
    tags.push(t("gemini.rumor_tag"));
  }
  const lowered = `${draftInput.title}\n${draftInput.body_text}`.toLowerCase();
  const keywordTags = [
    ["patch", "патч"],
    ["balance", "баланс"],
    ["skin", "скін"],
    ["costume", "костюм"],
    ["event", "івент"],
    ["season", "сезон"],
    ["map", "карта"],
    ["mode", "режим"],
    ["hero", "герой"],
    ["bug", "виправлення"],
    ["fix", "виправлення"],
  ];
  for (const [keyword, tag] of keywordTags) {
    if (lowered.includes(keyword) && !tags.includes(tag)) {
      tags.push(tag);
    }
  }

  return tags;
}

function isOfficialSource(draftInput) {
  return OFFICIAL_SOURCE_TYPES.has(draftInput.source_type);
}

function isWikiFactSource(draftInput) {
  return WIKI_FACT_SOURCE_TYPES.has(draftInput.source_type);
}

function prepareOfficialDraft(draftText, draftInput, { maxPartLength }) {
  const collapsedDraft = collapsePostParts(draftText);
  const result = sanitizeOfficialPublicDraft(collapsedDraft, draftInput);
  const maxLength = Math.min(officialMaxLength(draftInput), maxPartLength);
  return enforceOfficialLength(result, draftInput, { maxLength });
}

function sanitizeOfficialPublicDraft(draft, draftInput) {
  let result = cleanModelDraft(draft, draftInput);
  result = removeOfficialPublicMetadata(result, draftInput);
  result = applyDatetimeNotes(result, draftInput.datetime_notes);
  result = stripUnsupportedMarkdown(result);
  result = stripRawUrls(result);
  result = removeOfficialPublicMetadata(result, draftInput);
  result = ensureOfficialSourceAttribution(result, draftInput);
  result = ensureHashtagsAtBottom(result, draftInput);
  return normalizeBlankLines(result);
}

function publicHashtagLine(draftInput) {
  if (isOfficialSource(draftInput)) {
    return officialHashtags(draftInput).join(" ");
  }

  // The trivia rubric gets its own fixed tag. The news topic rules are keyed on
  // patch/event/shop wording that comic-history trivia never contains, so it fell
  // through to the "#Анонс" default — labelling a 1941 comics fact an announcement.
  if (isWikiFactSource(draftInput)) {
    return t("collectors.wiki_facts.hashtags");
  }

  // A leak is never an announcement, so rumour sources carry a permanent
  // "#Чутки" marker instead of the "#Анонс" default. It is added on EVERY such
  // post, not only when no topic matched: a marker that appears at random is one
  // readers cannot rely on, and as a constant it doubles as a channel-wide
  // filter separating rumours from official news.
  if (isRumorSource(draftInput)) {
    return [t("gemini.hashtags"), t("gemini.rumor_hashtag"), ...topicHashtags(draftInput, { fallback: false })].join(
      " ",
    );
  }

  // Non-official (social/short-form) posts get the base tag PLUS the same topic
  // tags, just without the #Офіційно marker, so they are tagged too — not bare.
  return [t("gemini.hashtags"), ...topicHashtags(draftInput)].join(" ");
}

function officialDatabaseTags(draftInput) {
  return officialHashtags(draftInput).map((tag) => tag.replace(/^#+/, ""));
}

/**
 * Up to three topic tags matched from the title/body.
 *
 * `fallback` adds "#Анонс" when nothing matched. Callers that already supply
 * their own marker (rumour sources) turn it off, so a leak is never labelled an
 * announcement just because no keyword hit.
 */
function topicHashtags(draftInput, { fallback = true } = {}) {
  const text = `${draftInput.title}\n${draftInput.body_text}`.toLowerCase();
  const topicTags = [];
  for (const [keywords, hashtag] of OFFICIAL_TOPIC_TAG_RULES) {
    if (topicTags.includes(hashtag)) {
      continue;
    }
    if (keywords.some((keyword) => matchesKeyword(text, keyword))) {
      topicTags.push(hashtag);
    }
    if (topicTags.length >= 3) {
      break;
    }
  }

  if (!topicTags.length && fallback) {
    topicTags.push("#Анонс");
  }

  return topicTags.slice(0, 3);
}

function officialHashtags(draftInput) {
  return [...OFFICIAL_BASE_HASHTAGS, ...topicHashtags(draftInput)];
}

function officialPostType(draftInput) {
  const text = `${draftInput.title}\n${draftInput.body_text}`.toLowerCase();
  for (const [postType, , keywords] of OFFICIAL_POST_TYPE_RULES) {
    if (keywords.some((keyword) => matchesKeyword(text, keyword))) {
      return postType;
    }
  }

  return "announcement";
}

function officialPostTypeLabel(draftInput) {
  const postType = officialPostType(draftInput);
  for (const [ruleType, label] of OFFICIAL_POST_TYPE_RULES) {
    if (ruleType === postType) {
      return label;
    }
  }

  return "Short announcement";
}

function officialMaxLength(draftInput) {
  if (officialPostType(draftInput) === "patch") {
    return OFFICIAL_PATCH_MAX_LENGTH;
  }

  return OFFICIAL_NORMAL_MAX_LENGTH;
}

function sourceAttributionLine(draftInput) {
  if (isOfficialSource(draftInput)) {
    return OFFICIAL_SOURCE_ATTRIBUTION;
  }

  return t("gemini.source_line", { source_name: draftInput.source_name });
}

function hasSourceAttribution(text) {
  const lowered = text.toLowerCase();
  return lowered.includes("офіційному сайті") || lowered.includes("джерело:");
}

function removeOfficialPublicMetadata(text, draftInput) {
  const lines = [];
  for (const line of text.split("\n")) {
    const stripped = line.trim();
    if (!stripped) {
      lines.push("");
      continue;
    }

    if (isOfficialMetadataLine(stripped, draftInput)) {
      continue;
    }

    lines.push(stripped);
  }

  return normalizeBlankLines(lines.join("\n"));
}

function isOfficialMetadataLine(line, draftInput) {
  const lowered = line.toLowerCase().trim();
  const normalized = strip(lowered, ":-—– ");
  if (!normalized) {
    return false;
  }

  if (draftInput.article_date_display && normalized === draftInput.article_date_display.toLowerCase()) {
    return true;
  }

  if (draftInput.article_url && lowered.includes(draftInput.article_url.toLowerCase())) {
    return true;
  }

  const metadataPrefixes = [
    "дата публікації",
    "дата статті",
    "оригінальна дата",
    "дата статті після",
    "джерело",
    "url",
    "source",
    "source url",
    "article date",
    "publication date",
    "посилання",
    "тип джерела",
    "назва джерела",
  ];
  if (metadataPrefixes.some((prefix) => normalized.startsWith(prefix))) {
    return true;
  }

  const sourceName = draftInput.source_name.toLowerCase().trim();
  if (sourceName && (normalized === sourceName || normalized === "офіційний сайт marvel rivals")) {
    return true;
  }

  return false;
}

function ensureOfficialSourceAttribution(text, draftInput) {
  const sourceLine = sourceAttributionLine(draftInput);
  const body = removeSourceAttributionLines(text);
  if (!body) {
    return sourceLine;
  }

  return `${body}\n\n${sourceLine}`;
}

function removeSourceAttributionLines(text) {
  const lines = [];
  for (const line of text.split("\n")) {
    const stripped = line.trim();
    if (!stripped) {
      lines.push("");
      continue;
    }

    const lowered = stripped.toLowerCase();
    if (lowered.includes("офіційному сайті") && (lowered.includes("повні деталі") || lowered.includes("<a"))) {
      continue;
    }

    if (looksLikeSourceOnlyLine(stripped)) {
      continue;
    }

    lines.push(stripped);
  }

  return normalizeBlankLines(lines.join("\n"));
}

function ensureHashtagsAtBottom(text, draftInput) {
  const hashtags = publicHashtagLine(draftInput);
  const bodyLines = [];
  for (const line of text.split("\n")) {
    const stripped = line.trim();
    if (!stripped) {
      bodyLines.push("");
      continue;
    }

    if (isHashtagOnlyLine(stripped) || stripped.toLowerCase().startsWith("теги:")) {
      continue;
    }

    bodyLines.push(stripped);
  }

  const body = normalizeBlankLines(bodyLines.join("\n"));
  if (!body) {
    return hashtags;
  }

  return `${body}\n\n${hashtags}`;
}

function collapsePostParts(draft) {
  const parts = draft
    .split(POST_SEPARATOR)
    .map((part) => part.trim())
    .filter(Boolean);
  if (!parts.length) {
    return draft.trim();
  }

  return parts.slice(0, 2).join("\n\n").trim();
}

function enforceOfficialLength(text, draftInput, { maxLength }) {
  let result = normalizeBlankLines(text);
  if (charLength(result) <= maxLength) {
    return result;
  }

  const hashtags = publicHashtagLine(draftInput);
  const sourceLine = sourceAttributionLine(draftInput);
  let body = removeExactLine(result, hashtags);
  body = removeExactLine(body, sourceLine).trim();

  const suffix = [sourceLine, hashtags].filter(Boolean).join("\n\n");
  let available = Math.max(260, maxLength - charLength(suffix) - 4);
  let shortened = truncatePlainText(body, available).trim();
  if (sourceLine && !shortened.includes(sourceLine)) {
    shortened = `${shortened}\n\n${sourceLine}`.trim();
  }

  result = `${shortened}\n\n${hashtags}`.trim();
  if (charLength(result) <= maxLength) {
    return normalizeBlankLines(result);
  }

  available = Math.max(180, maxLength - charLength(hashtags) - 4);
  shortened = truncatePlainText(shortened, available);
  return normalizeBlankLines(`${shortened}\n\n${hashtags}`);
}

function matchesKeyword(text, keyword) {
  if (keyword.includes(" ")) {
    return text.includes(keyword);
  }

  return new RegExp(`(?<!${WORD})${escapeRegExp(keyword)}(?!${WORD})`, "u").test(text);
}

function stripUnsupportedMarkdown(text) {
  let result = replaceOfficialSourceAnchor(text);
  result = result.replace(/<\/?(?:b|strong|i|em|u|s|span|h[1-6])[^>]*>/gi, "");
  result = result.replace(/<[^>\n]+>/g, "");
  result = result.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  result = result.replace(/\*\*(.*?)\*\*/g, "$1");
  result = result.replace(/__(.*?)__/g, "$1");
  result = result.replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "$1");
  result = result.replaceAll("`", "");
  result = result.replace(/^\s*[*-]\s+/gm, "");
  result = result.replace(/^\s*\*\s*$/gm, "");
  result = result.replace(/\*{2,}/g, "");
  return result;
}

function stripRawUrls(text) {
  let result = replaceOfficialSourceAnchor(text);
  result = result.replace(/\s*https?:\/\/\S+/g, "");
  result = result.replace(/\s*www\.\S+/g, "");
  result = result.replace(/\s*(?:деталі|повні деталі|details|source|link|url|посилання)\s*:\s*$/i, "");
  return result;
}

function replaceOfficialSourceAnchor(text) {
  OFFICIAL_SOURCE_LINK_PATTERN.lastIndex = 0;
  return text.replace(OFFICIAL_SOURCE_LINK_PATTERN, "офіційному сайті");
}

function looksLikeSourceOnlyLine(text) {
  const normalized = strip(text.trim(), ":-—– ").toLowerCase();
  return ["джерело", "source", "url", "link", "посилання", "деталі", "повні деталі"].includes(normalized);
}

function removeExactLine(text, lineToRemove) {
  if (!lineToRemove) {
    return text;
  }

  return text
    .split("\n")
    .filter((line) => line.trim() !== lineToRemove)
    .join("\n");
}

function truncatePlainText(text, maxLength) {
  if (charLength(text) <= maxLength) {
    return text;
  }

  const ending = "…\n\nПовні деталі — на офіційному сайті.";
  const available = Math.max(80, maxLength - charLength(ending));
  let truncated = rstrip(sliceChars(text, 0, available));
  let splitAt = Math.max(
    rfindChars(truncated, "\n\n"),
    rfindChars(truncated, ". "),
    rfindChars(truncated, "! "),
    rfindChars(truncated, "? "),
  );
  if (splitAt > Math.floor(available / 2)) {
    truncated = rstrip(sliceChars(truncated, 0, splitAt + 1));
  } else {
    splitAt = rfindChars(truncated, " ");
    if (splitAt > Math.floor(available / 2)) {
      truncated = rstrip(sliceChars(truncated, 0, splitAt));
    }
  }

  return `${truncated}${ending}`;
}

function isHashtagOnlyLine(value) {
  return Boolean(value.startsWith("#") && /^(?:#[^\s#]+)(?:\s+#[^\s#]+)*$/.test(value));
}

function normalizeBlankLines(text) {
  return text.replace(/\n{3,}/g, "\n\n").trim();
}

function applyDatetimeNotes(text, datetimeNotes) {
  if (!text || !datetimeNotes) {
    return text;
  }

  let result = text;
  for (const line of datetimeNotes.split("\n")) {
    const match =
      /^\s*-\s*(?<original>\d{1,2}:\d{2}\s*(?:UTC|GMT)(?:\s*[+-]\s*\d{1,2}(?::?[0-5]\d)?)?)\s*\([^)]+\)\s*->\s*(?<converted>.+?)\s*$/i.exec(
        line,
      );
    if (match === null) {
      continue;
    }

    const original = match.groups.original;
    const converted = match.groups.converted.trim();
    result = result.replace(new RegExp(escapeRegExp(original), "gi"), converted);
  }

  return result;
}

function splitDraftParts(draft, { maxPartLength }) {
  let explicitParts = draft
    .split(POST_SEPARATOR)
    .map((part) => part.trim())
    .filter(Boolean);
  if (!explicitParts.length) {
    explicitParts = [draft.trim()];
  }

  const normalizedParts = [];
  for (const part of explicitParts) {
    normalizedParts.push(...splitOversizedPart(part, { maxPartLength }));
  }

  return normalizedParts.length ? normalizedParts : [draft.trim()];
}

function splitOversizedPart(part, { maxPartLength }) {
  if (charLength(part) <= maxPartLength) {
    return [part];
  }

  let paragraphs = part
    .split("\n\n")
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  if (!paragraphs.length) {
    paragraphs = [part.trim()];
  }

  const chunks = [];
  let current = "";
  for (const paragraph of paragraphs) {
    const candidate = current ? `${current}\n\n${paragraph}`.trim() : paragraph;
    if (charLength(candidate) <= maxPartLength) {
      current = candidate;
      continue;
    }

    if (current) {
      chunks.push(current);
    }
    current = paragraph;

    while (charLength(current) > maxPartLength) {
      let splitAt = rfindChars(current, "\n", maxPartLength);
      if (splitAt < Math.floor(maxPartLength / 2)) {
        splitAt = rfindChars(current, " ", maxPartLength);
      }
      if (splitAt < Math.floor(maxPartLength / 2)) {
        splitAt = maxPartLength;
      }

      chunks.push(sliceChars(current, 0, splitAt).trim());
      current = sliceChars(current, splitAt).trim();
    }
  }

  if (current) {
    chunks.push(current);
  }

  if (chunks.length <= 1) {
    return chunks;
  }

  return chunks.map((chunk, index) => prefixPart(chunk, index + 1));
}

function prefixPart(part, number) {
  const [firstLine, separator, rest] = partition(part, "\n");
  const partTitle = t("gemini.part_title", { number });
  if (firstLine.startsWith("📰")) {
    return `${firstLine} · ${partTitle}${separator}${rest}`.trim();
  }

  return `📰 ${partTitle}\n\n${part}`.trim();
}

const templateCache = new Map();

function loadCachedText(file, { trim = true, fallback = null } = {}) {
  if (templateCache.has(file)) {
    return templateCache.get(file);
  }
  let value;
  try {
    // Python read these in text mode, which normalises line endings. Node does
    // not, so on a CRLF checkout every prompt sent to Gemini would silently
    // differ from the one this repo was tuned against.
    const raw = fs.readFileSync(file, "utf8").replaceAll("\r\n", "\n");
    value = trim ? raw.trim() : raw;
  } catch (error) {
    if (fallback === null) {
      throw error;
    }
    logger.warning(`Official news style prompt ${file} is unavailable`);
    value = fallback;
  }
  templateCache.set(file, value);
  return value;
}

function loadPromptTemplate() {
  return loadCachedText(PROMPT_PATH);
}

function loadShortFormPromptTemplate() {
  return loadCachedText(SHORT_FORM_PROMPT_PATH);
}

function loadWikiFactPromptTemplate() {
  return loadCachedText(WIKI_FACT_PROMPT_PATH);
}

function loadDedupPromptTemplate() {
  // Read verbatim (no trim): the dedup prompt is used as-is, exactly as the
  // Python loader did for this one file.
  return loadCachedText(DEDUP_PROMPT_PATH, { trim: false });
}

function loadWikiPickPromptTemplate() {
  return loadCachedText(WIKI_PICK_PROMPT_PATH);
}

function loadStylePrompt() {
  return loadCachedText(STYLE_PROMPT_PATH, {
    fallback: "Створи короткий Telegram-пост українською: 400-900 символів, без URL, з тегами в кінці.",
  });
}

// Exported for the unit tests, which exercise the pure text pipeline without
// touching the model.
export const __testing = {
  appendExtraLinks,
  cleanModelDraft,
  cleanResponseText,
  collapsePostParts,
  ensureRequiredMetadata,
  enforceOfficialLength,
  extractTags,
  fallbackTags,
  isHashtagOnlyLine,
  matchesKeyword,
  normalizeBlankLines,
  normalizeTags,
  prepareOfficialDraft,
  publicHashtagLine,
  splitDraftParts,
  stripRawUrls,
  stripUnsupportedMarkdown,
  applyDatetimeNotes,
  truncatePlainText,
  buildPrompt,
  buildDedupPrompt,
  geminiRetryDelaySeconds,
  isRateLimitError,
  isRetryableGeminiError,
  loadShortFormPromptTemplate,
  selectPromptTemplate,
  sourceAttributionLine,
};
