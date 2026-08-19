/**
 * Tests for the community-footer rendering, especially that the footer is located
 * by a structural sentinel — so an untrusted post body containing the visible
 * footer text can no longer suppress, hijack or forge the footer.
 */

import { expect, it } from "vitest";

import { formatCommunityFooter, formatCommunityFooterHtml, formatPostHtml } from "../services/post_footer.js";

const CHAT_LINK = '<a href="https://t.me/UAMarvelRivalsChat">Чат</a>';
const SUBMISSION_LINK = '<a href="https://t.me/MarvelRivalsUABot">Запропонувати новину</a>';
const DISCORD_LINK = '<a href="https://discord.gg/U8HvUB7NFt">Discord</a>';
// The footer is one line of links now — no rule above it, no heading — so every
// label ends up inside an anchor and none of it survives as plain text. That is
// what an attacker would have to reproduce to forge it.
const FOOTER_TEXT = formatCommunityFooter();
// The private-use marker post_footer places before the appended footer. It is
// intentionally not exported: these tests assert it never reaches the output.
const FOOTER_SENTINEL = "";

function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

it("appends the footer as one line of links", () => {
  const html = formatPostHtml("Свіжа новина.", { include_community_footer: true });

  expect(html).toContain(CHAT_LINK);
  expect(html).toContain(SUBMISSION_LINK);
  expect(html).toContain(DISCORD_LINK);
  expect(html).not.toContain(FOOTER_SENTINEL); // the marker never reaches output
});

it("carries no rule and no heading above the links", () => {
  const html = formatPostHtml("Свіжа новина.", { include_community_footer: true });

  expect(FOOTER_TEXT).toBe("💬 Чат | 🤖 Запропонувати новину | 🎧 Discord");
  expect(html).not.toContain("─");
  expect(html).not.toContain("Навігація");
  // Body, one blank line, links — nothing between them.
  expect(html.startsWith("Свіжа новина.\n\n💬 ")).toBe(true);
});

it("does not let a body containing the footer text suppress the footer", () => {
  // The attack: a feed body that embeds the visible footer text used to make the
  // old substring check think the footer was already present and skip it.
  const malicious = `Дивіться: ${FOOTER_TEXT} (підробка) і ще текст.`;
  const html = formatPostHtml(malicious, { include_community_footer: true });

  // The real footer is still appended with working links...
  expect(html).toContain(CHAT_LINK);
  // ...and the label now appears at least twice: the attacker's escaped copy plus
  // the genuine appended footer (i.e. it was NOT suppressed).
  expect(countOccurrences(html, "Запропонувати новину")).toBeGreaterThanOrEqual(2);
  expect(html).not.toContain(FOOTER_SENTINEL);
});

it("strips a sentinel injected into the body", () => {
  const html = formatPostHtml(`зло${FOOTER_SENTINEL}текст`, { include_community_footer: true });
  expect(html).not.toContain(FOOTER_SENTINEL); // an injected sentinel cannot survive
  expect(html).toContain(CHAT_LINK); // and cannot move/suppress the real footer
});

it("adds no footer when it was not requested", () => {
  const html = formatPostHtml("Просто текст.", { include_community_footer: false });

  expect(html).not.toContain(CHAT_LINK);
  expect(html).not.toContain("Запропонувати новину");
  expect(html).not.toContain(FOOTER_SENTINEL);
});

it("linkifies the standalone footer helper without leaking the sentinel", () => {
  // The album digest caption builds its own HTML via this helper.
  const html = formatCommunityFooterHtml();

  expect(html).toContain(CHAT_LINK);
  expect(html).toContain(SUBMISSION_LINK);
  expect(html).toContain(DISCORD_LINK);
  expect(html).not.toContain(FOOTER_SENTINEL);
});
