/**
 * Weekly "Чи знали ви?" hero-trivia rubric, sourced from the Marvel Rivals
 * Fandom wiki (CC BY-SA).
 *
 * It is registered in the registry with scheduled=false: that keeps it out of the
 * per-tick news run (which would post a fact every interval instead of weekly)
 * while still offering it as a manual /fetch_news button. It runs on its own
 * weekly schedule, like the fan-art digest, and reuses the shared
 * BaseNewsCollector pipeline so each English trivia fact is translated to
 * Ukrainian by Gemini and queued for moderation, credited to the wiki with a link
 * to the source page. It opts out of cross-source dedup (a fact is not a news
 * story).
 */

import crypto from "node:crypto";

import { DateTime } from "luxon";

import { cancellableSleep, createTask } from "../../background.js";
import { nextWeeklyRunAt, resolveTimezone } from "../../digests/fanart.js";
import { t } from "../../i18n.js";
import { getLogger } from "../../logger.js";
import { collapseWhitespace } from "../../pyutils.js";
import { CollectionMode, collectorDefinition, draftCandidate, listingEntry } from "../base.js";
import { BaseNewsCollector } from "../runner.js";
import { WikiFactsClient } from "./client.js";

const logger = getLogger("services.collectors.wiki_facts.collector");

export const COLLECTOR_ID = "wiki_facts";
export const SOURCE_TYPE = "wiki_facts";

export const DEFINITION = collectorDefinition({
  collector_id: COLLECTOR_ID,
  source_type: SOURCE_TYPE,
  title_key: "collectors.wiki_facts.title",
  button_key: "buttons.collector_wiki_facts",
});

export class WikiFactsCollector extends BaseNewsCollector {
  static definition = DEFINITION;
  // A trivia fact must never be dropped as a "duplicate" of a news story, nor
  // suppress one.
  static participates_in_cross_source_dedup = false;

  constructor({ config, db, bot }) {
    super({ config, db, bot });
    this.client = new WikiFactsClient(config.wiki_facts_api_url);
  }

  missingGeminiWarning() {
    return t("collectors.wiki_facts.errors.missing_gemini_api_key");
  }

  async fetchListing() {
    const heroes = await this.client.fetchHeroTitles();
    if (!heroes.length) {
      return [];
    }

    shuffleInPlace(heroes);
    const entries = [];
    for (const hero of heroes) {
      const facts = await this.client.fetchTriviaFacts(hero);
      const heroEntries = facts.map((fact) => listingEntry(factId(fact), fact));
      entries.push(...heroEntries);
      // Stop as soon as a hero contributes an UNSEEN fact — the common case is
      // the first hero, so we rarely scan more than one. But keep walking the
      // (shuffled) roster while everything seen so far is already posted, so a
      // depleted random sample never silently starves the weekly rubric; we
      // only give up once the whole roster is exhausted.
      if (await this.hasUnseen(heroEntries)) {
        break;
      }
    }
    return entries;
  }

  async hasUnseen(entries) {
    for (const entry of entries) {
      if (!(await this.db.isSourceSeen(SOURCE_TYPE, entry.dedup_key))) {
        return true;
      }
    }
    return false;
  }

  async parseEntry(entry) {
    const fact = entry.payload;
    return draftCandidate({
      source_id: entry.dedup_key,
      source_url: fact.page_url,
      title: t("collectors.wiki_facts.fact_title", { hero: fact.hero }),
      body_text: fact.fact,
      source_name: t("collectors.wiki_facts.source_name"),
      username: t("collectors.wiki_facts.username"),
      original_text: buildOriginalText(fact),
      article_date: null,
      article_date_display: null,
      has_media: false,
      media_url: null,
      media_type: "none",
      additional_media_urls: null,
    });
  }
}

/** Fisher-Yates, the direct equivalent of `random.shuffle`. */
function shuffleInPlace(values) {
  for (let index = values.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1));
    [values[index], values[swap]] = [values[swap], values[index]];
  }
}

// Exported for the unit tests, which assert the id is whitespace/case stable.
export const __testing = { factId };

function factId(fact) {
  const digest = crypto
    .createHash("sha1")
    .update(collapseWhitespace(fact.fact).toLowerCase(), "utf8")
    .digest("hex")
    .slice(0, 16);
  return `${fact.hero}:${digest}`;
}

function buildOriginalText(fact) {
  return [
    t("collectors.common.original_text.article_title", { value: fact.hero }),
    t("collectors.common.original_text.article_url", { value: fact.page_url }),
    "",
    t("collectors.common.original_text.parsed_article_text"),
    fact.fact,
  ]
    .join("\n")
    .trim();
}

// --- weekly scheduler ----------------------------------------------------------

/** Start the weekly wiki-facts loop, or return null when it is off / unusable. */
export function startWikiFactsScheduler(bot, config, db) {
  if (!config.enable_wiki_facts) {
    logger.info("Weekly wiki-facts rubric is disabled (ENABLE_WIKI_FACTS).");
    return null;
  }
  if (!config.gemini_api_key) {
    logger.warning("GEMINI_API_KEY is missing; wiki-facts needs translation and is disabled.");
    return null;
  }

  logger.info(
    `Weekly wiki-facts enabled: weekday ${config.wiki_facts_weekday} at ` +
      `${String(config.wiki_facts_hour).padStart(2, "0")}:00 ${config.article_timezone}`,
  );
  return createTask("wiki-facts-scheduler", (signal) => wikiFactsLoop(bot, config, db, signal));
}

async function wikiFactsLoop(bot, config, db, signal) {
  const zone = resolveTimezone(config.article_timezone);
  for (;;) {
    const now = DateTime.now().setZone(zone);
    const nextRun = nextWeeklyRunAt(now, config.wiki_facts_weekday, config.wiki_facts_hour);
    await cancellableSleep(Math.max(1.0, nextRun.diff(now).as("seconds")), signal);
    try {
      await runWikiFactsOnce(bot, config, db);
    } catch (error) {
      logger.exception("Weekly wiki-facts run failed; retrying next week.", error);
    }
  }
}

/**
 * Queue one translated "Чи знали ви?" fact for moderation. Returns true when a
 * submission was created.
 */
export async function runWikiFactsOnce(bot, config, db) {
  const collector = new WikiFactsCollector({ config, db, bot });
  const stats = await collector.runOnce(CollectionMode.MANUAL_LATEST);
  if (stats.sent_to_moderation === 0 && stats.failed === 0) {
    // Make a no-op run observable rather than a silent week-long gap.
    logger.warning(
      `Wiki-facts run produced no post (found=${stats.found}) — every sampled fact is ` +
        "already seen or none had a Trivia section.",
    );
  } else {
    logger.info(
      `Wiki-facts run: found=${stats.found} new=${stats.new} ` +
        `sent=${stats.sent_to_moderation} failed=${stats.failed}`,
    );
  }
  return stats.sent_to_moderation > 0;
}
