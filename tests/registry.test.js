/**
 * Tests for opt-in collector gating: Bluesky is registered but only surfaces /
 * runs when ENABLE_BLUESKY_SOURCE is on; the official source is always available.
 */

import { afterEach, expect, it, vi } from "vitest";

import { collectionStats } from "../services/collectors/base.js";
import * as registry from "../services/collectors/registry.js";
import { OfficialMarvelRivalsCollector } from "../services/collectors/official_marvel_rivals/collector.js";
import { BaseNewsCollector } from "../services/collectors/runner.js";
import { sleep } from "../services/pyutils.js";

const {
  createAllCollectors,
  createCollector,
  listCollectorDefinitions,
  runAllCollectors,
} = registry;

afterEach(() => {
  vi.restoreAllMocks();
});

function config({ bluesky, youtube = false, reddit = false, rivalskins = false, wikiFacts = false }) {
  return {
    enable_bluesky_source: bluesky,
    bluesky_actor: "marvelrivalsglobal.bsky.social",
    enable_youtube_source: youtube,
    youtube_channel_id: "UCWzmOSSiSPbVnVu3ZAyDx2w",
    youtube_exclude_keywords: new Set(["esports"]),
    enable_reddit_source: reddit,
    reddit_subreddit: "MarvelRivalsLeaks",
    reddit_flairs: ["Official News", "Reliable", "Confirmed"],
    reddit_exclude_keywords: new Set(["megathread"]),
    enable_rivalskins_source: rivalskins,
    rivalskins_feed_url: "https://rivalskins.com/category/leaks/feed/",
    enable_wiki_facts: wikiFacts,
    wiki_facts_api_url: "https://marvelrivals.fandom.com/api.php",
    official_news_url: "https://www.marvelrivals.com/news/",
    article_timezone: "Europe/Kyiv",
  };
}

function idsFor(cfg) {
  return new Set(listCollectorDefinitions(cfg).map((definition) => definition.collector_id));
}

it("hides Bluesky when disabled", () => {
  const ids = idsFor(config({ bluesky: false }));
  expect(ids.has("bluesky")).toBe(false);
  expect(ids.has("official_marvel_rivals")).toBe(true);
});

it("shows Bluesky when enabled", () => {
  expect(idsFor(config({ bluesky: true })).has("bluesky")).toBe(true);
});

const gatedSources = [
  ["youtube", "youtube"],
  ["reddit", "reddit"],
  ["rivalskins", "rivalskins"],
  ["wiki_facts", "wikiFacts"],
];

it.each(gatedSources)("hides %s when disabled and shows it when enabled", (collectorId, flag) => {
  expect(idsFor(config({ bluesky: false, [flag]: false })).has(collectorId)).toBe(false);
  expect(idsFor(config({ bluesky: false, [flag]: true })).has(collectorId)).toBe(true);
});

it.each(gatedSources)("gates createCollector for %s", (collectorId, flag) => {
  expect(createCollector(collectorId, { config: config({ bluesky: false, [flag]: false }), db: null, bot: null })).toBeNull();
  const collector = createCollector(collectorId, {
    config: config({ bluesky: false, [flag]: true }),
    db: null,
    bot: null,
  });
  expect(collector).not.toBeNull();
  expect(collector.definition.collector_id).toBe(collectorId);
});

it("never joins wiki facts to the news tick", () => {
  // The rubric is weekly and has its own scheduler. It is a manual /fetch_news
  // button only — joining the tick would post a fact every interval instead.
  const ids = createAllCollectors({
    config: config({ bluesky: true, wikiFacts: true }),
    db: null,
    bot: null,
  }).map((collector) => collector.definition.collector_id);
  expect(ids).not.toContain("wiki_facts");
  expect(ids).toContain("official_marvel_rivals"); // ordinary sources still run on the tick
});

it("lists every collector without a config", () => {
  const ids = idsFor(null);
  for (const id of ["official_marvel_rivals", "bluesky", "youtube", "reddit", "rivalskins", "wiki_facts"]) {
    expect(ids.has(id), id).toBe(true);
  }
});

it("gates createCollector for Bluesky", () => {
  expect(createCollector("bluesky", { config: config({ bluesky: false }), db: null, bot: null })).toBeNull();
  const collector = createCollector("bluesky", { config: config({ bluesky: true }), db: null, bot: null });
  expect(collector).not.toBeNull();
  expect(collector.definition.collector_id).toBe("bluesky");
});

it("gates createAllCollectors", () => {
  const disabled = createAllCollectors({ config: config({ bluesky: false }), db: null, bot: null }).map(
    (collector) => collector.definition.collector_id,
  );
  expect(disabled).not.toContain("bluesky");
  expect(disabled).toContain("official_marvel_rivals");

  const enabled = createAllCollectors({ config: config({ bluesky: true }), db: null, bot: null }).map(
    (collector) => collector.definition.collector_id,
  );
  expect(enabled).toContain("bluesky");
});

it("keeps the official collector opted out of cross-source dedup", () => {
  expect(OfficialMarvelRivalsCollector.participates_in_cross_source_dedup).toBe(false);
  expect(BaseNewsCollector.participates_in_cross_source_dedup).toBe(true);
});

class FakeCollector {
  constructor(name, order) {
    this.definition = { collector_id: name };
    this.name = name;
    this._order = order;
  }

  async runOnce() {
    this._order.push(`${this.name}:start`);
    await sleep(0);
    this._order.push(`${this.name}:end`);
    return collectionStats({ collector_id: this.name, source_type: this.name, source_title: this.name });
  }
}

function tickConfig(interval = 0) {
  return { moderation_send_interval_seconds: interval };
}

it("runs collectors sequentially", async () => {
  // Sequential execution is what lets each collector's cross-source dedup see the
  // previous collector's just-marked titles. If they ran concurrently, the yield
  // inside runOnce would interleave the start/end markers.
  const order = [];
  const collectors = [new FakeCollector("a", order), new FakeCollector("b", order)];

  const stats = await runAllCollectors({ config: tickConfig(), db: null, bot: null, collectors });

  expect(order).toEqual(["a:start", "a:end", "b:start", "b:end"]);
  expect(stats.map((entry) => entry.collector_id)).toEqual(["a", "b"]);
});

it("shares one throttle across the tick", async () => {
  // Every collector in a tick must get the SAME throttle, built from the config
  // interval, so the inter-send gap is honoured across sources, not reset per source.
  const collectors = [new FakeCollector("a", []), new FakeCollector("b", [])];

  await runAllCollectors({ config: tickConfig(7), db: null, bot: null, collectors });

  expect(collectors[0].throttle).toBe(collectors[1].throttle);
  expect(collectors[0].throttle.min_interval_seconds).toBe(7);
});
