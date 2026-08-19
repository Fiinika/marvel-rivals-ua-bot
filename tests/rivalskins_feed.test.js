/**
 * Tests for the rivalskins.com leaks collector: WordPress-RSS parsing, picking the
 * skin-render image (skipping the recurring banner, rejecting off-site hosts),
 * pubDate normalisation, and the collector -> DraftCandidate mapping (a rumour source).
 */

import { expect, it } from "vitest";

import { listingEntry } from "../services/collectors/base.js";
import { RivalSkinsCollector } from "../services/collectors/rivalskins/collector.js";
import { normalizePubDate, parseFeed } from "../services/collectors/rivalskins/feed_fetcher.js";
import { RUMOR_SOURCE_TYPES } from "../services/gemini.js";

const HERO = "https://rivalskins.com/wp-content/uploads/2026/06/Phoenix-White-Crown-Phoenix.jpg";
const BANNER = "https://rivalskins.com/wp-content/uploads/2026/06/s85-launch-skins-1024x576.jpg";
const OFFSITE = "https://evil.example.com/x.jpg";

function itemXml(
  title,
  {
    postId,
    link = "https://rivalskins.com/leaks/phoenix-white-crown-phoenix/",
    pubdate = "Tue, 09 Jun 2026 20:07:38 +0000",
    imgs = [HERO],
    body = "Новий скін Фенікс.",
  },
) {
  const imgHtml = imgs.map((url) => `<img src="${url}"/>`).join("");
  return (
    "<item>" +
    `<title>${title}</title>` +
    `<link>${link}</link>` +
    `<pubDate>${pubdate}</pubDate>` +
    `<guid isPermaLink="false">${postId}</guid>` +
    `<content:encoded><![CDATA[<div>${imgHtml}<p>${body}</p></div>]]></content:encoded>` +
    "</item>"
  );
}

function feedXml(...items) {
  return (
    '<?xml version="1.0" encoding="UTF-8"?>' +
    '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">' +
    `<channel><title>RivalSkins</title>${items.join("")}</channel></rss>`
  );
}

it("extracts the fields and the hero image", () => {
  const posts = parseFeed(
    feedXml(itemXml("Phoenix: White Crown Phoenix", { postId: "https://rivalskins.com/?p=4798" })),
  );

  expect(posts).toHaveLength(1);
  const post = posts[0];
  expect(post.title).toBe("Phoenix: White Crown Phoenix");
  expect(post.post_id).toBe("https://rivalskins.com/?p=4798");
  expect(post.web_url).toBe("https://rivalskins.com/leaks/phoenix-white-crown-phoenix/");
  expect(post.image_url).toBe(HERO);
  expect(post.body_text).toContain("Новий скін Фенікс.");
  expect(post.created_at).toBe("2026-06-09T20:07:38+00:00");
});

it("skips the recurring banner and picks the render", () => {
  // Banner listed FIRST must be skipped in favour of the unique skin render.
  const posts = parseFeed(feedXml(itemXml("Magik Skin", { postId: "p1", imgs: [BANNER, HERO] })));
  expect(posts[0].image_url).toBe(HERO);
  expect(posts[0].additional_image_urls).toEqual([]);
});

it("keeps every render of a post for the album", () => {
  const second = "https://rivalskins.com/wp-content/uploads/2026/06/Phoenix-back.jpg";
  const posts = parseFeed(feedXml(itemXml("Phoenix", { postId: "p3", imgs: [HERO, second] })));

  expect(posts[0].image_url).toBe(HERO);
  expect(posts[0].additional_image_urls).toEqual([second]);
});

it("drops an image that repeats across posts", () => {
  // The site ends most posts with the season-roadmap promo of the moment. It is
  // not that post's content, and hardcoding its filename would not survive the
  // next season — recurrence within one fetch is the durable signal.
  const promo = "https://rivalskins.com/wp-content/uploads/2026/08/s10-roadmap-1024x576.jpg";
  const otherHero = "https://rivalskins.com/wp-content/uploads/2026/08/Ultron-street-style.jpg";
  const posts = parseFeed(
    feedXml(
      itemXml("Phoenix", { postId: "p4", imgs: [HERO, promo] }),
      itemXml("Ultron", { postId: "p5", imgs: [otherHero, promo] }),
    ),
  );

  expect(posts[0].image_url).toBe(HERO);
  expect(posts[0].additional_image_urls).toEqual([]);
  expect(posts[1].image_url).toBe(otherHero);
  expect(posts[1].additional_image_urls).toEqual([]);
});

it("leaves a post with nothing but a recurring image media-less", () => {
  const promo = "https://rivalskins.com/wp-content/uploads/2026/08/s10-roadmap-1024x576.jpg";
  const posts = parseFeed(
    feedXml(itemXml("A", { postId: "p6", imgs: [promo] }), itemXml("B", { postId: "p7", imgs: [promo] })),
  );

  expect(posts[0].image_url).toBeNull();
  expect(posts[1].image_url).toBeNull();
});

it("rejects an off-site image host", () => {
  const posts = parseFeed(feedXml(itemXml("Skin", { postId: "p2", imgs: [OFFSITE] })));
  expect(posts[0].image_url).toBeNull();
});

it("dedups by guid", () => {
  const posts = parseFeed(feedXml(itemXml("First", { postId: "same" }), itemXml("Dup", { postId: "same" })));
  expect(posts).toHaveLength(1);
});

it("returns nothing for invalid XML", () => {
  expect(parseFeed("not xml")).toEqual([]);
  expect(parseFeed("")).toEqual([]);
});

it("rejects an entity-expansion bomb", () => {
  // The feed is untrusted: a DTD with nested entities (billion-laughs) must be
  // refused by the parser, not expanded — parseFeed returns [].
  const bomb =
    '<?xml version="1.0"?>' +
    '<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">' +
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">' +
    '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>' +
    '<rss version="2.0"><channel><item><title>&c;</title></item></channel></rss>';
  expect(parseFeed(bomb)).toEqual([]);
});

it("normalises an RFC-822 pubDate to a UTC ISO timestamp", () => {
  expect(normalizePubDate("Tue, 09 Jun 2026 20:07:38 +0000")).toBe("2026-06-09T20:07:38+00:00");
  // A non-UTC offset is converted to UTC.
  expect(normalizePubDate("Tue, 09 Jun 2026 23:07:38 +0300")).toBe("2026-06-09T20:07:38+00:00");
  expect(normalizePubDate("")).toBeNull();
  expect(normalizePubDate("garbage")).toBeNull();
});

it("treats rivalskins as a rumour source", () => {
  expect(RUMOR_SOURCE_TYPES.has("rivalskins")).toBe(true);
});

it("maps a post to a rumour photo candidate", async () => {
  const post = {
    post_id: "https://rivalskins.com/?p=1",
    web_url: "https://rivalskins.com/leaks/x/",
    title: "Magik: Soulless Sword",
    body_text: "Опис скіна Magik.",
    created_at: "2026-06-09T20:07:38+00:00",
    image_url: HERO,
    additional_image_urls: [],
  };
  const collector = new RivalSkinsCollector({
    config: { rivalskins_feed_url: "https://rivalskins.com/category/leaks/feed/" },
    db: null,
    bot: null,
  });

  const candidate = await collector.parseEntry(listingEntry(post.post_id, post));

  expect(candidate.source_id).toBe(post.post_id);
  expect(candidate.source_url).toBe(post.web_url);
  expect(candidate.title).toBe("Magik: Soulless Sword");
  expect(candidate.has_media).toBe(true);
  expect(candidate.media_url).toBe(HERO);
  expect(candidate.media_type).toBe("photo");
  expect(candidate.article_date_display).toBe("2026-06-09");
  expect(candidate.source_name).toBe("RivalSkins");
});
