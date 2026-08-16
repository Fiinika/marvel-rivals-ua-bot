/**
 * Tests for the community-footer rendering, especially that the footer is located
 * by a structural sentinel — so an untrusted post body containing the visible
 * footer title can no longer suppress, hijack or forge the footer.
 */

import { expect, it } from "vitest";

import { t } from "../services/i18n.js";
import { formatCommunityFooterHtml, formatPostHtml } from "../services/post_footer.js";

const CHAT_LINK = '<a href="https://t.me/UAMarvelRivalsChat">Чат</a>';
const FOOTER_TITLE = t("post_footer.title");
// The private-use marker post_footer places before the appended footer. It is
// intentionally not exported: these tests assert it never reaches the output.
const FOOTER_SENTINEL = "";

function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

it("appends and linkifies the footer", () => {
  const html = formatPostHtml("Свіжа новина.", { include_community_footer: true });
  expect(html).toContain(FOOTER_TITLE);
  expect(html).toContain(CHAT_LINK); // footer links are turned into anchors
  expect(html).not.toContain(FOOTER_SENTINEL); // the marker never reaches output
});

it("does not let a body containing the footer title suppress the footer", () => {
  // The attack: a feed body that embeds the visible footer title used to make the
  // old title-substring check think the footer was already present and skip it.
  const malicious = `Дивіться: ${FOOTER_TITLE} (підробка) і ще текст.`;
  const html = formatPostHtml(malicious, { include_community_footer: true });

  // The real footer is still appended with working links...
  expect(html).toContain(CHAT_LINK);
  // ...and the title phrase now appears at least twice: the attacker's escaped copy
  // plus the genuine appended footer (i.e. it was NOT suppressed).
  expect(countOccurrences(html, FOOTER_TITLE)).toBeGreaterThanOrEqual(2);
  expect(html).not.toContain(FOOTER_SENTINEL);
});

it("strips a sentinel injected into the body", () => {
  const html = formatPostHtml(`зло${FOOTER_SENTINEL}текст`, { include_community_footer: true });
  expect(html).not.toContain(FOOTER_SENTINEL); // an injected sentinel cannot survive
  expect(html).toContain(CHAT_LINK); // and cannot move/suppress the real footer
});

it("adds no footer when it was not requested", () => {
  const html = formatPostHtml("Просто текст.", { include_community_footer: false });
  expect(html).not.toContain(FOOTER_TITLE);
  expect(html).not.toContain(FOOTER_SENTINEL);
});

it("linkifies the standalone footer helper without leaking the sentinel", () => {
  // The album digest caption builds its own HTML via this helper.
  const html = formatCommunityFooterHtml();
  expect(html).toContain(CHAT_LINK);
  expect(html).toContain(FOOTER_TITLE);
  expect(html).not.toContain(FOOTER_SENTINEL);
});
