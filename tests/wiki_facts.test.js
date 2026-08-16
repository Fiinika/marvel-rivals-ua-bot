/**
 * Tests for the weekly wiki-facts rubric: Trivia wikitext cleaning, the collector
 * mapping (a non-dedup'd "Чи знали ви?" candidate), the /wikifact admin command,
 * and the dedicated Gemini prompt.
 */

import { afterEach, expect, it, vi } from "vitest";

import { buildAdminComposer } from "../handlers/admin.js";
import { listingEntry } from "../services/collectors/base.js";
import { BaseNewsCollector } from "../services/collectors/runner.js";
import { extractFacts } from "../services/collectors/wiki_facts/client.js";
import * as wikiCollector from "../services/collectors/wiki_facts/collector.js";
import { WikiFactsCollector } from "../services/collectors/wiki_facts/collector.js";
import { __testing, geminiDraftInput } from "../services/gemini.js";
import { t } from "../services/i18n.js";
import { dispatch, fakeBot, messageUpdate, sentTexts } from "./helpers/telegram.js";

const { buildPrompt, fallbackTags, publicHashtagLine, selectPromptTemplate } = __testing;

/** The collector's fact-id helper, re-derived here so the module stays private. */
const factId = wikiCollector.__testing.factId;

afterEach(() => {
  vi.restoreAllMocks();
});

/**
 * Make the collector's Fisher-Yates roster shuffle a no-op.
 *
 * Python could monkeypatch `random.shuffle` itself; JavaScript cannot intercept a
 * module-local function, so the randomness is pinned at its source instead. A
 * value just under 1 makes `floor(r * (i + 1)) === i`, i.e. every element swaps
 * with itself and the order is preserved.
 */
function stubShuffleAsIdentity() {
  vi.spyOn(Math, "random").mockReturnValue(0.999999);
}

function wikiFact(hero, fact, pageUrl) {
  return { hero, fact, page_url: pageUrl };
}

it("cleans Trivia wikitext", () => {
  const wikitext =
    "* [[Adam Warlock]]'s first appearance was in '''Fantastic Four''' (1961) #66.<ref name=x>cite</ref>\n" +
    "* His staff is called the [[Karmic Staff|Karmic Staff]].\n" +
    "* {{Template|junk}}Short\n" + // too short after template strip -> dropped
    "* See [https://example.com/x the source] for more about Limbo time travel mechanics here.\n";
  const facts = extractFacts(wikitext);

  expect(facts[0]).toBe("Adam Warlock's first appearance was in Fantastic Four (1961) #66.");
  expect(facts[1]).toBe("His staff is called the Karmic Staff.");
  // external-link markup reduced to its label; the too-short bullet was dropped.
  expect(facts.some((fact) => fact.includes("the source for more about Limbo"))).toBe(true);
  expect(facts.every((fact) => !fact.includes("[[") && !fact.includes("<ref") && !fact.includes("{{"))).toBe(true);
  expect(facts).not.toContain("Short");
});

it("unwraps nested templates", () => {
  const raw = "{{Quote|text with {{inner}} bits}} this is a real factual sentence about the hero.";
  expect(extractFacts(`* ${raw}`)).toEqual(["this is a real factual sentence about the hero."]);
});

it("drops residual template markup", () => {
  // A template nested deeper than the unwrap cap leaves braces -> the bullet is
  // dropped rather than published garbled.
  const deep = `* ${"{{".repeat(8)}x${"}}".repeat(8)} trailing text that is long enough to pass the filter here`;
  expect(extractFacts(deep)).toEqual([]);
});

it("strips media links and keeps display text", () => {
  const raw = "See [[File:foo.png|thumb|200px|Caption]] and [[Hero Page|Magik]] do cool things together here.";
  const cleaned = extractFacts(`* ${raw}`)[0];
  expect(cleaned).not.toContain("thumb");
  expect(cleaned).not.toContain("200px");
  expect(cleaned).not.toContain("File:");
  expect(cleaned).toContain("Magik"); // display text (last pipe segment) kept
});

it("strips bracketed and raw URLs", () => {
  expect(extractFacts("* A fact with a bare link [https://evil.example.com] inside it here.")[0]).not.toContain("http");
  expect(
    extractFacts("* A fact with a raw https://evil.example.com url inside it right here.")[0],
  ).not.toContain("http");
});

it("derives a stable, whitespace-insensitive fact id", () => {
  const a = wikiFact("Magik", "Illyana   first appeared in 1975.", "https://w/Magik");
  const b = wikiFact("Magik", "illyana first appeared in 1975.", "https://w/Magik");
  expect(factId(a)).toBe(factId(b));
  expect(factId(a).startsWith("Magik:")).toBe(true);
});

class FakeDb {
  constructor(seen = []) {
    this.seen = new Set(seen);
  }

  async isSourceSeen(_sourceType, sourceId) {
    return this.seen.has(sourceId);
  }
}

function buildCollector(facts, { db = new FakeDb() } = {}) {
  const collector = new WikiFactsCollector({
    config: { wiki_facts_api_url: "https://w/api.php" },
    db,
    bot: null,
  });
  collector.client = {
    async fetchHeroTitles() {
      return Object.keys(facts);
    },
    async fetchTriviaFacts(hero) {
      return facts[hero] ?? [];
    },
  };
  return collector;
}

// --- the /wikifact admin command -------------------------------------------------

function wikiConfig({ enabled = true, geminiKey = "k", admins = [7] } = {}) {
  return {
    enable_wiki_facts: enabled,
    gemini_api_key: geminiKey,
    admin_user_ids: new Set(admins),
    admin_chat_id: 100,
    telegram_moderation_chat_ids: new Set(),
  };
}

async function runWikiFactCommand({ config, result, userId = 7, chatId = 100, chatType = "private" }) {
  const calls = [];
  vi.spyOn(wikiCollector, "runWikiFactsOnce").mockImplementation(async () => {
    calls.push("ran");
    if (result instanceof Error) {
      throw result;
    }
    return result;
  });

  const bot = fakeBot();
  const composer = buildAdminComposer({ config, db: {}, bot });
  await dispatch(composer, messageUpdate({ text: "/wikifact", userId, chatId, chatType }), bot);
  return { calls, answers: sentTexts(bot) };
}

it("reports the rubric being switched off", async () => {
  // The whole point of the command: a disabled rubric is diagnosable from Telegram
  // instead of only from the server log at startup.
  const { calls, answers } = await runWikiFactCommand({ config: wikiConfig({ enabled: false }), result: true });

  expect(calls).toEqual([]); // never touches the wiki when the feature is off
  expect(answers).toEqual([t("admin.wiki_fact.disabled")]);
  expect(answers[0]).toContain("ENABLE_WIKI_FACTS");
});

it("reports a missing Gemini key", async () => {
  const { calls, answers } = await runWikiFactCommand({ config: wikiConfig({ geminiKey: "" }), result: true });

  expect(calls).toEqual([]);
  expect(answers).toEqual([t("admin.wiki_fact.no_gemini")]);
});

it("queues a fact", async () => {
  const { calls, answers } = await runWikiFactCommand({ config: wikiConfig(), result: true });

  expect(calls).toEqual(["ran"]);
  expect(answers).toEqual([t("admin.wiki_fact.started"), t("admin.wiki_fact.queued")]);
});

it("reports when nothing was queued", async () => {
  const { answers } = await runWikiFactCommand({ config: wikiConfig(), result: false });
  expect(answers).toEqual([t("admin.wiki_fact.started"), t("admin.wiki_fact.skipped")]);
});

it("reports a failed run", async () => {
  const { answers } = await runWikiFactCommand({ config: wikiConfig(), result: new Error("wiki down") });
  expect(answers).toEqual([t("admin.wiki_fact.started"), t("admin.wiki_fact.failed")]);
});

it("rejects non-admins", async () => {
  const { calls, answers } = await runWikiFactCommand({ config: wikiConfig(), result: true, userId: 999 });

  expect(calls).toEqual([]);
  expect(answers).toEqual([t("admin.wiki_fact.no_permission")]);
});

it("does not answer /wikifact in a non-admin group chat", async () => {
  // The chat-scope filter declines the update entirely, so it falls through to the
  // next composer instead of being answered here.
  const { calls, answers } = await runWikiFactCommand({
    config: wikiConfig(),
    result: true,
    chatId: 555,
    chatType: "supergroup",
  });

  expect(calls).toEqual([]);
  expect(answers).toEqual([]);
});

it("opts the collector out of cross-source dedup", () => {
  expect(WikiFactsCollector.participates_in_cross_source_dedup).toBe(false);
  expect(BaseNewsCollector.participates_in_cross_source_dedup).toBe(true);
});

it("stops at the first hero with an unseen fact", async () => {
  // Deterministic order; all facts unseen -> stop after the first hero (cheap).
  stubShuffleAsIdentity();
  const f1 = wikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik");
  const f2 = wikiFact("Blade", "Blade is a Dhampir, half human and half vampire.", "https://w/Blade");
  const entries = await buildCollector({ Magik: [f1], Blade: [f2] }).fetchListing();

  expect(entries.map((entry) => entry.dedup_key)).toEqual([factId(f1)]);
});

it("keeps scanning when the first heroes are all seen", async () => {
  // Anti-starvation: the first hero's only fact is already seen, so the scan must
  // continue to the next hero rather than give up.
  stubShuffleAsIdentity();
  const f1 = wikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik");
  const f2 = wikiFact("Blade", "Blade is a Dhampir, half human and half vampire.", "https://w/Blade");
  const db = new FakeDb([factId(f1)]);
  const entries = await buildCollector({ Magik: [f1], Blade: [f2] }, { db }).fetchListing();

  expect(entries.map((entry) => entry.dedup_key)).toEqual([factId(f1), factId(f2)]);
});

it("builds a did-you-know candidate", async () => {
  const fact = wikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik");
  const collector = buildCollector({});
  const entry = listingEntry(factId(fact), fact);

  const candidate = await collector.parseEntry(entry);

  expect(candidate.source_id).toBe(factId(fact)); // == dedup_key, so the seen-check is consistent
  expect(candidate.source_url).toBe("https://w/Magik");
  expect(candidate.body_text).toBe(fact.fact);
  expect(candidate.source_name).toBe("Marvel Rivals Wiki (CC BY-SA)");
  expect(candidate.title).toContain("Magik");
  expect(candidate.has_media).toBe(false);
});

function wikiDraftInput(title, body) {
  return geminiDraftInput({
    title,
    article_url: "https://w/Hero",
    article_date_display: null,
    datetime_notes: "",
    body_text: body,
    source_type: "wiki_facts",
    source_name: "Marvel Rivals Wiki (CC BY-SA)",
  });
}

it("selects and renders the wiki-fact prompt", () => {
  const draftInput = wikiDraftInput("Цікавий факт про Magik", "Magik can teleport through Limbo stepping discs.");
  const template = selectPromptTemplate(draftInput);
  expect(template).toContain("Чи знали ви");

  const prompt = buildPrompt(draftInput);
  // The wiki prompt is chosen and renders (all placeholders present) with the fact,
  // the CC BY-SA attribution and the injection guard.
  expect(prompt).toContain("Чи знали ви");
  expect(prompt).toContain("Magik can teleport");
  expect(prompt).toContain("CC BY-SA");
  expect(prompt).toContain("ВАЖЛИВО ПРО БЕЗПЕКУ");
});

it("uses its own rubric hashtag", () => {
  // A comics-history fact matches no news topic rule, so it used to fall through
  // to the "#Анонс" default — calling a 1941 comic debut an announcement.
  const line = publicHashtagLine(
    wikiDraftInput(
      "Цікавий факт про Winter Soldier",
      'Bucky Barnes first appeared in "Captain America Comics" #1 in 1941.',
    ),
  );

  expect(line).toBe("#MarvelRivalsUA #ЧиЗналиВи");
  expect(line).not.toContain("#Анонс");
});

it("ignores incidental news keywords in a fact", () => {
  // A fact mentioning a hero/skin/map must still be tagged as trivia, not as a
  // skin or map news post.
  const line = publicHashtagLine(
    wikiDraftInput(
      "Цікавий факт про Magik",
      "This hero's costume design references a map from the Limbo season of the comics.",
    ),
  );
  expect(line).toBe("#MarvelRivalsUA #ЧиЗналиВи");
});

it("keeps the stored tags in step with the rubric", () => {
  const draftInput = wikiDraftInput("Цікавий факт про Magik", "Magik wields the Soulsword.");
  expect(fallbackTags(draftInput)).toEqual(["marvelrivalsua", "чизналиви"]);
});

it("leaves the social short-form tagging unchanged", () => {
  // The trivia branch must not alter how the social/leak sources are tagged.
  const social = geminiDraftInput({
    title: "New skin bundle",
    article_url: "https://bsky.app/x",
    article_date_display: null,
    datetime_notes: "",
    body_text: "A new costume bundle arrives in the shop.",
    source_type: "bluesky",
    source_name: "Bluesky Marvel Rivals",
  });
  const line = publicHashtagLine(social);

  expect(line.startsWith("#MarvelRivalsUA")).toBe(true);
  expect(line).toContain("#Скіни");
  expect(line).not.toContain("#ЧиЗналиВи");
  expect(fallbackTags(social)).not.toContain("чизналиви");
});
