/**
 * Unit tests for the lenient env parsers config.js exposes through loadConfig,
 * which moderation and the nightly backup rely on.
 *
 * The Python version poked at private module functions via monkeypatch; here the
 * parsers are exercised through `loadConfig(env)` with an explicit environment
 * object, so no test can leak into (or read from) the real process env.
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_FANART_DIGEST_HOUR,
  DEFAULT_FANART_DIGEST_WEEKDAY,
  DEFAULT_FANART_FLAIR,
  DEFAULT_FANART_SUBREDDIT,
  DEFAULT_REDDIT_EXCLUDE_KEYWORDS,
  DEFAULT_REDDIT_FLAIRS,
  DEFAULT_REDDIT_SUBREDDIT,
  DEFAULT_WIKI_FACTS_API_URL,
  DEFAULT_WIKI_FACTS_HOUR,
  DEFAULT_WIKI_FACTS_WEEKDAY,
  DEFAULT_YOUTUBE_CHANNEL_ID,
  DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS,
  loadConfig,
} from "../config.js";

const REQUIRED = {
  BOT_TOKEN: "token",
  ADMIN_CHAT_ID: "1",
  PUBLISH_CHAT_ID: "2",
  ADMIN_USER_IDS: "3",
};

function load(overrides = {}) {
  return loadConfig({ ...REQUIRED, ...overrides });
}

describe("invite allowlist", () => {
  it("normalises URLs and case", () => {
    const config = load({
      TELEGRAM_LINK_ALLOWLIST:
        "https://t.me/UAMarvelRivals, MarvelRivalsUABot, https://discord.gg/ExampleInvite/, t.me/+AbCdEf123,, ",
    });
    expect([...config.telegram_link_allowlist].sort()).toEqual(
      ["+abcdef123", "exampleinvite", "marvelrivalsuabot", "uamarvelrivals"].sort(),
    );
  });

  it("is empty when unset", () => {
    expect(load().telegram_link_allowlist.size).toBe(0);
  });
});

it("parses a chat-id list and skips bad entries", () => {
  // Deliberately placeholder ids: a test never needs the community's real ones.
  const config = load({ TELEGRAM_MODERATION_CHAT_IDS: "-1001234567890, oops, , -42" });
  expect([...config.telegram_moderation_chat_ids].sort()).toEqual([-1001234567890, -42].sort());
});

describe("optional booleans", () => {
  const cases = [
    ["true", true],
    ["TRUE", true],
    ["1", true],
    ["yes", true],
    ["on", true],
    ["false", false],
    ["FALSE", false],
    ["0", false],
    ["no", false],
    ["off", false],
  ];

  it.each(cases)("reads %s as %s", (value, expected) => {
    expect(load({ ENABLE_DISCORD_MODERATION: value }).enable_discord_moderation).toBe(expected);
  });

  it("keeps the default on a typo, so a flag never silently flips", () => {
    // Default-off flag stays off, default-on flag stays on.
    expect(load({ ENABLE_DISCORD_MODERATION: "nonsense" }).enable_discord_moderation).toBe(false);
    expect(load({ ENABLE_DATABASE_BACKUP: "nonsense" }).enable_database_backup).toBe(true);
  });

  it("keeps the default when empty or missing", () => {
    expect(load({ ENABLE_DATABASE_BACKUP: "" }).enable_database_backup).toBe(true);
    expect(load().enable_database_backup).toBe(true);
    expect(load().enable_discord_moderation).toBe(false);
  });
});

describe("optional hour", () => {
  const cases = [
    ["0", 0],
    ["4", 4],
    ["23", 23],
    ["24", 4], // out of range -> default
    ["-1", 4],
    ["abc", 4],
    ["", 4],
  ];

  it.each(cases)("reads %s as %s", (value, expected) => {
    expect(load({ DATABASE_BACKUP_HOUR: value }).database_backup_hour).toBe(expected);
  });

  it("defaults when missing", () => {
    expect(load().database_backup_hour).toBe(4);
  });
});

it("defaults the submission limits and send interval", () => {
  const config = load();
  expect(config.min_submission_text_words).toBe(3);
  expect(config.min_submission_text_chars).toBe(10);
  expect(config.moderation_send_interval_seconds).toBe(5);
});

it("honours submission-limit and send-interval overrides", () => {
  const config = load({
    MIN_SUBMISSION_TEXT_WORDS: "5",
    MIN_SUBMISSION_TEXT_CHARS: "0", // 0 disables the char floor
    MODERATION_SEND_INTERVAL_SECONDS: "8",
  });
  expect(config.min_submission_text_words).toBe(5);
  expect(config.min_submission_text_chars).toBe(0);
  expect(config.moderation_send_interval_seconds).toBe(8);
});

it("keeps the default send interval on a typo", () => {
  // Parsed leniently (like the other scheduler settings): a typo must never stop
  // the bot, it just keeps the default pacing.
  expect(load({ MODERATION_SEND_INTERVAL_SECONDS: "soon" }).moderation_send_interval_seconds).toBe(5);
});

it("enables cross-source dedup by default", () => {
  const config = load();
  expect(config.enable_cross_source_dedup).toBe(true);
  expect(config.cross_source_dedup_title_limit).toBe(200);
});

it("honours cross-source dedup overrides", () => {
  const config = load({ ENABLE_CROSS_SOURCE_DEDUP: "false", CROSS_SOURCE_DEDUP_TITLE_LIMIT: "50" });
  expect(config.enable_cross_source_dedup).toBe(false);
  expect(config.cross_source_dedup_title_limit).toBe(50);
});

it("defaults the YouTube source", () => {
  const config = load();
  expect(config.enable_youtube_source).toBe(false);
  expect(config.youtube_channel_id).toBe(DEFAULT_YOUTUBE_CHANNEL_ID);
  expect([...config.youtube_exclude_keywords].sort()).toEqual([...DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS].sort());
  expect(config.enable_youtube_video_download).toBe(true);
  expect(config.youtube_video_max_mb).toBe(48);
});

it("can disable the YouTube video download", () => {
  const config = load({ ENABLE_YOUTUBE_VIDEO_DOWNLOAD: "false", YOUTUBE_VIDEO_MAX_MB: "20" });
  expect(config.enable_youtube_video_download).toBe(false);
  expect(config.youtube_video_max_mb).toBe(20);
});

it("honours YouTube overrides", () => {
  const config = load({
    ENABLE_YOUTUBE_SOURCE: "true",
    YOUTUBE_CHANNEL_ID: "UCcustom",
    YOUTUBE_EXCLUDE_KEYWORDS: "Foo, BAR ,baz",
  });
  expect(config.enable_youtube_source).toBe(true);
  expect(config.youtube_channel_id).toBe("UCcustom");
  expect([...config.youtube_exclude_keywords].sort()).toEqual(["bar", "baz", "foo"]);
});

it("can switch the YouTube exclude list off entirely", () => {
  expect(load({ YOUTUBE_EXCLUDE_KEYWORDS: "-" }).youtube_exclude_keywords.size).toBe(0);
});

it("defaults and overrides the Bluesky video settings", () => {
  const defaults = load();
  expect(defaults.enable_bluesky_video_download).toBe(true);
  expect(defaults.bluesky_video_max_mb).toBe(48);

  const overridden = load({ ENABLE_BLUESKY_VIDEO_DOWNLOAD: "false", BLUESKY_VIDEO_MAX_MB: "20" });
  expect(overridden.enable_bluesky_video_download).toBe(false);
  expect(overridden.bluesky_video_max_mb).toBe(20);
});

it("defaults the Reddit source", () => {
  const config = load();
  expect(config.enable_reddit_source).toBe(false);
  expect(config.reddit_subreddit).toBe(DEFAULT_REDDIT_SUBREDDIT);
  expect(config.reddit_flairs).toEqual(DEFAULT_REDDIT_FLAIRS);
  expect([...config.reddit_exclude_keywords].sort()).toEqual([...DEFAULT_REDDIT_EXCLUDE_KEYWORDS].sort());
});

it("honours Reddit overrides", () => {
  const config = load({
    ENABLE_REDDIT_SOURCE: "true",
    REDDIT_SUBREDDIT: "MarvelRivals",
    REDDIT_FLAIRS: "Patch Notes, Esports",
  });
  expect(config.enable_reddit_source).toBe(true);
  expect(config.reddit_subreddit).toBe("MarvelRivals");
  expect(config.reddit_flairs).toEqual(["Patch Notes", "Esports"]);
});

it("defaults and overrides wiki facts", () => {
  const defaults = load();
  expect(defaults.enable_wiki_facts).toBe(false);
  expect(defaults.wiki_facts_weekday).toBe(DEFAULT_WIKI_FACTS_WEEKDAY);
  expect(defaults.wiki_facts_hour).toBe(DEFAULT_WIKI_FACTS_HOUR);
  expect(defaults.wiki_facts_api_url).toBe(DEFAULT_WIKI_FACTS_API_URL);

  const overridden = load({ ENABLE_WIKI_FACTS: "true", WIKI_FACTS_WEEKDAY: "3", WIKI_FACTS_HOUR: "9" });
  expect(overridden.enable_wiki_facts).toBe(true);
  expect(overridden.wiki_facts_weekday).toBe(3);
  expect(overridden.wiki_facts_hour).toBe(9);
});

it("defaults the fan-art digest", () => {
  const config = load();
  expect(config.enable_fanart_digest).toBe(false);
  expect(config.fanart_subreddit).toBe(DEFAULT_FANART_SUBREDDIT);
  expect(config.fanart_flair).toBe(DEFAULT_FANART_FLAIR);
  expect(config.fanart_digest_weekday).toBe(DEFAULT_FANART_DIGEST_WEEKDAY);
  expect(config.fanart_digest_hour).toBe(DEFAULT_FANART_DIGEST_HOUR);
  expect(config.fanart_digest_count).toBe(10);
});

it("clamps the fan-art digest count to ten", () => {
  const config = load({
    ENABLE_FANART_DIGEST: "true",
    FANART_DIGEST_COUNT: "50",
    FANART_DIGEST_WEEKDAY: "6",
  });
  expect(config.enable_fanart_digest).toBe(true);
  expect(config.fanart_digest_count).toBe(10); // capped at Telegram's media-group max
  expect(config.fanart_digest_weekday).toBe(6);
});

it("keeps the default weekday when the value is out of range", () => {
  expect(load({ FANART_DIGEST_WEEKDAY: "9" }).fanart_digest_weekday).toBe(DEFAULT_FANART_DIGEST_WEEKDAY);
});

it("rejects a non-integer chat id outright", () => {
  expect(() => load({ ADMIN_CHAT_ID: "not-a-number" })).toThrow(/ADMIN_CHAT_ID must be an integer/);
});

it("requires every mandatory variable", () => {
  expect(() => loadConfig({ ...REQUIRED, BOT_TOKEN: "  " })).toThrow(/Missing required environment variable/);
});
