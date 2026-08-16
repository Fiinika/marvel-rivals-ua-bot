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
import { GeminiDraftGenerator } from "../../gemini.js";
import { t } from "../../i18n.js";
import { getLogger } from "../../logger.js";
import { collapseWhitespace, errorText } from "../../pyutils.js";
import { CollectionMode, collectorDefinition, draftCandidate, listingEntry } from "../base.js";
import { BaseNewsCollector } from "../runner.js";
import { WikiFactsClient } from "./client.js";
import { FactTier, rejectReason, tierOf } from "./quality.js";

const logger = getLogger("services.collectors.wiki_facts.collector");

export const COLLECTOR_ID = "wiki_facts";
export const SOURCE_TYPE = "wiki_facts";

// How wide to cast the net before choosing. A hero has a median of 7 bullets, so
// three heroes usually fill the shortlist; the cap keeps a bad week (heroes with
// only nested bullets left) from walking the whole roster.
const SHORTLIST_SIZE = 8;
const MIN_HEROES_SCANNED = 3;
const MAX_HEROES_SCANNED = 8;
const MAX_PER_HERO = 2;

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

  /**
   * Return every fact scanned this run, ordered so the one we WANT published is
   * the first unseen element.
   *
   * The runner publishes `entries[first unseen]`, so this method is the only
   * place selection can happen. It walks a shuffled slice of the roster, keeps
   * the bullets that can stand on their own as a post (see quality.js), builds a
   * shortlist, and puts the winner in front of the other unseen entries. The
   * already-seen entries stay at the head of the list so `stats.found` and
   * `stats.duplicates` still describe the whole scan.
   */
  async fetchListing() {
    const heroes = await this.client.fetchPageTitles();
    if (!heroes.length) {
      return [];
    }

    shuffleInPlace(heroes);
    const seenEntries = [];
    const scanned = [];
    let eligibleFound = 0;
    for (const hero of heroes) {
      const facts = await this.client.fetchTriviaFacts(hero);
      const unseen = [];
      let seenCount = 0;
      for (const fact of facts) {
        const entry = listingEntry(factId(fact), fact);
        if (await this.db.isSourceSeen(SOURCE_TYPE, entry.dedup_key)) {
          seenEntries.push(entry);
          seenCount += 1;
          continue;
        }
        unseen.push({ entry, tier: rejectReason(fact) === null ? tierOf(fact) : null });
      }
      if (!unseen.length) {
        continue;
      }
      scanned.push({ seen_count: seenCount, unseen });
      eligibleFound += unseen.filter((item) => item.tier !== null && item.tier !== FactTier.FORMULAIC).length;
      // Enough to choose from, and from enough different heroes to make the
      // choice a real one. The roster walk continues past a fully-seen hero, so
      // a depleted sample still cannot starve the weekly rubric.
      if (eligibleFound >= SHORTLIST_SIZE && scanned.length >= MIN_HEROES_SCANNED) {
        break;
      }
      if (scanned.length >= MAX_HEROES_SCANNED) {
        break;
      }
    }

    if (!scanned.length) {
      return seenEntries;
    }

    const shortlist = buildShortlist(scanned);
    const rest = scanned.flatMap((hero) => hero.unseen.map((item) => item.entry));
    if (!shortlist.length) {
      // Every unseen bullet this run is a nested/dangling one. Publishing an
      // orphan reads worse than skipping, so hand the runner only seen entries
      // and let runWikiFactsOnce log the empty week.
      return seenEntries;
    }

    const winner = await this.chooseFact(shortlist);
    return [...seenEntries, winner, ...rest.filter((entry) => entry !== winner)];
  }

  /**
   * Pick one entry out of the shortlist.
   *
   * Gemini reads the shortlist and picks the fact that works best as a "Чи знали
   * ви?" post — it catches what a regex cannot (a sentence that only makes sense
   * next to its neighbour, a fact that is really two facts, a joke that does not
   * survive translation). It fails open to the shortlist head, which is already
   * the highest-tier fact of the freshest hero.
   */
  async chooseFact(shortlist) {
    if (!this.config.gemini_api_key || shortlist.length === 1) {
      return shortlist[0];
    }
    const generator = new GeminiDraftGenerator(this.config.gemini_api_key, this.config.gemini_model);
    try {
      const index = await generator.pickBestTriviaFact(shortlist.map((entry) => entry.payload.fact));
      return shortlist[index] ?? shortlist[0];
    } catch (error) {
      logger.warning(`Wiki-fact shortlist pick failed; using the top-ranked fact. ${errorText(error)}`);
      return shortlist[0];
    }
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

/**
 * Order the shortlist: freshest hero first (fewest facts of theirs already
 * published, which spreads the rubric across the roster instead of mining one
 * hero), then by tier, and at most MAX_PER_HERO from any one hero so a
 * 104-bullet page like Deadpool cannot fill the whole shortlist.
 */
function buildShortlist(scanned) {
  const byFreshness = [...scanned].sort((left, right) => left.seen_count - right.seen_count);
  const shortlist = [];
  for (const tier of [FactTier.INTERESTING, FactTier.PLAIN, FactTier.NESTED, FactTier.FORMULAIC]) {
    for (const hero of byFreshness) {
      const matching = hero.unseen.filter((item) => item.tier === tier);
      shuffleInPlace(matching);
      for (const item of matching.slice(0, MAX_PER_HERO)) {
        if (shortlist.length < SHORTLIST_SIZE) {
          shortlist.push(item.entry);
        }
      }
    }
    if (shortlist.length >= SHORTLIST_SIZE) {
      break;
    }
  }
  return shortlist;
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
