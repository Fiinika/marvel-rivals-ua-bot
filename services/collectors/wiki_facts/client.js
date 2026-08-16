/**
 * Fetch hero "Trivia" facts from the Marvel Rivals Fandom wiki via api.php.
 *
 * The wiki content is CC BY-SA, so every published fact must credit the wiki with
 * a link to the source page (handled by the collector's attribution line). This
 * client only reads: it lists the heroes in Category:Heroes and extracts the
 * cleaned bullet points of each hero's "Trivia" section.
 */

import { getLogger } from "../../logger.js";
import { collapseWhitespace, errorText } from "../../pyutils.js";
import { quoteUrlPath } from "../../urlutils.js";

const logger = getLogger("services.collectors.wiki_facts.client");

export const DEFAULT_API_URL = "https://marvelrivals.fandom.com/api.php";
export const DEFAULT_PAGE_BASE = "https://marvelrivals.fandom.com/wiki/";
export const HEROES_CATEGORY = "Category:Heroes";
export const USER_AGENT = "MarvelRivalsUACollector/1.0 (Telegram news bot; +https://t.me/MarvelRivalsUABot)";
export const REQUEST_TIMEOUT_SECONDS = 25.0;

// A trivia bullet must be a real sentence, not a stray fragment or a giant essay.
const MIN_FACT_LENGTH = 25;
const MAX_FACT_LENGTH = 600;

const REF_RE = /<ref[^>]*>[\s\S]*?<\/ref>|<ref[^>]*\/>/gi;
// Innermost {{...}}; applied repeatedly to unwrap nested templates.
const TEMPLATE_RE = /\{\{[^{}]*\}\}/g;
const TEMPLATE_MAX_PASSES = 5;
// Whole [[...]] inner; resolved by wikilinkText (drops File:/Image:/Category:,
// else keeps the display text after the last pipe).
const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;
const MEDIA_NAMESPACES = new Set(["file", "image", "category"]);
// [https://url optional label] — label kept when present, else dropped.
const EXTLINK_RE = /\[https?:\/\/\S+(?:\s+([^\]]+))?\]/g;
// Bare/raw URLs left after the bracket forms.
const RAW_URL_RE = /https?:\/\/\S+|www\.\S+/gi;
const HTML_TAG_RE = /<[^>]+>/g;
const BULLET_RE = /^\*+[^\S\n]*(.+?)[^\S\n]*$/gm;

/** @typedef {{hero: string, fact: string, page_url: string}} WikiFact */

export class WikiFactsClient {
  constructor(apiUrl = DEFAULT_API_URL, { page_base = DEFAULT_PAGE_BASE } = {}) {
    this.api_url = apiUrl;
    this.page_base = page_base;
  }

  async fetchHeroTitles() {
    const data = await this.get({
      action: "query",
      list: "categorymembers",
      cmtitle: HEROES_CATEGORY,
      cmlimit: "500",
      cmtype: "page",
    });
    if (data === null) {
      return [];
    }
    const members = data.query?.categorymembers ?? [];
    // Drop the category's own landing page ("Heroes") and any blank title.
    const titles = [];
    for (const member of members) {
      if (member === null || typeof member !== "object" || Array.isArray(member)) {
        continue;
      }
      const title = String(member.title ?? "").trim();
      if (title && title !== "Heroes") {
        titles.push(title);
      }
    }
    return titles;
  }

  async fetchTriviaFacts(hero) {
    const sections = await this.get({ action: "parse", page: hero, prop: "sections" });
    if (sections === null) {
      return [];
    }
    const sectionList = sections.parse?.sections ?? [];
    const triviaSection = sectionList.find(
      (section) =>
        section !== null &&
        typeof section === "object" &&
        !Array.isArray(section) &&
        String(section.line ?? "").trim().toLowerCase() === "trivia",
    );
    const index = triviaSection === undefined ? null : String(triviaSection.index);
    if (!index) {
      return [];
    }

    const parsed = await this.get({ action: "parse", page: hero, section: index, prop: "wikitext" });
    if (parsed === null) {
      return [];
    }
    const wikitext = parsed.parse?.wikitext?.["*"] ?? "";

    const pageUrl = `${this.page_base}${quoteUrlPath(hero.replaceAll(" ", "_"))}`;
    return extractFacts(wikitext).map((fact) => Object.freeze({ hero, fact, page_url: pageUrl }));
  }

  async get(params) {
    const query = new URLSearchParams({ format: "json", ...params });
    try {
      const response = await fetch(`${this.api_url}?${query}`, {
        redirect: "follow",
        headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_SECONDS * 1000),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = await response.json();
      return data !== null && typeof data === "object" && !Array.isArray(data) ? data : null;
    } catch (error) {
      logger.warning(`Wiki API request failed (${params.action}): ${errorText(error)}`);
      return null;
    }
  }
}

export function extractFacts(wikitext) {
  const facts = [];
  BULLET_RE.lastIndex = 0;
  for (const match of String(wikitext ?? "").matchAll(BULLET_RE)) {
    const fact = cleanFact(match[1]);
    // Drop anything that still carries template markup (e.g. a template nested
    // deeper than the unwrap cap) rather than publish a garbled fact.
    if (fact.includes("{") || fact.includes("}")) {
      continue;
    }
    if (fact.length >= MIN_FACT_LENGTH && fact.length <= MAX_FACT_LENGTH) {
      facts.push(fact);
    }
  }
  return facts;
}

function cleanFact(raw) {
  let text = raw.replace(REF_RE, "");
  // Unwrap nested templates by repeating the innermost-match strip to a fixed point.
  for (let pass = 0; pass < TEMPLATE_MAX_PASSES; pass += 1) {
    const stripped = text.replace(TEMPLATE_RE, "");
    if (stripped === text) {
      break;
    }
    text = stripped;
  }
  text = text.replace(EXTLINK_RE, (_match, label) => label ?? "");
  text = text.replace(WIKILINK_RE, (_match, inner) => wikilinkText(inner));
  text = text.replace(RAW_URL_RE, "");
  text = text.replace(HTML_TAG_RE, "");
  text = text.replaceAll("'''", "").replaceAll("''", "");
  return collapseWhitespace(text).trim();
}

function wikilinkText(inner) {
  const target = inner.split("|")[0].trim();
  // Drop media/category links entirely; for normal links keep the display text
  // (the segment after the last pipe), so layout params like thumb|200px are gone.
  if (MEDIA_NAMESPACES.has(target.split(":")[0].trim().toLowerCase())) {
    return "";
  }
  const segments = inner.split("|");
  return segments[segments.length - 1].trim();
}
