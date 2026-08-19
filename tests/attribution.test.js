/**
 * Tests for per-source attribution: the source line is admin-only metadata, so a
 * public post carries none — except the official reader link and the wiki rubric's
 * CC BY-SA credit. Covers the Gemini source/hashtag lines, the shared
 * allow-source-link predicate, and the "Джерело: <name>" footer linkification.
 */

import { expect, it } from "vitest";

import { __testing, geminiDraftInput } from "../services/gemini.js";
import { OFFICIAL_SOURCE_ATTRIBUTION, formatPostHtml, submissionAllowsSourceLink } from "../services/post_footer.js";

const { buildPrompt, ensureRequiredMetadata, fallbackTags, publicHashtagLine, selectPromptTemplate, sourceAttributionLine } =
  __testing;

function draft(sourceType, sourceName = "MR Bluesky") {
  return geminiDraftInput({
    title: "title",
    article_url: "https://example.com/1",
    article_date_display: null,
    datetime_notes: null,
    body_text: "body",
    source_type: sourceType,
    source_name: sourceName,
  });
}

it("gives an ordinary collector post no public source line", () => {
  // The source is shown to moderators in the submission card, not to readers.
  expect(sourceAttributionLine(draft("bluesky", "MR Bluesky"))).toBe("");
  expect(sourceAttributionLine(draft("youtube", "YouTube Marvel Rivals"))).toBe("");
  expect(sourceAttributionLine(draft("reddit", "Reddit"))).toBe("");
  expect(sourceAttributionLine(draft("rivalskins", "RivalSkins"))).toBe("");
});

it("keeps the wiki credit, which CC BY-SA requires", () => {
  expect(sourceAttributionLine(draft("wiki_facts", "Marvel Rivals Wiki (CC BY-SA)"))).toBe(
    "Джерело: Marvel Rivals Wiki (CC BY-SA)",
  );
});

it("leaves official attribution unchanged", () => {
  expect(sourceAttributionLine(draft("official_marvel_rivals"))).toBe(OFFICIAL_SOURCE_ATTRIBUTION);
});

it("drops #Офіційно from non-official hashtags", () => {
  const line = publicHashtagLine(draft("bluesky"));
  expect(line).not.toContain("#Офіційно");
  expect(line).toContain("#MarvelRivalsUA");
  expect(line.replace("#MarvelRivalsUA", "")).toContain("#"); // also carries a topic tag now
});

it("gives non-official posts topic tags", () => {
  const line = publicHashtagLine(
    geminiDraftInput({
      title: "New trailer reveal",
      article_url: "https://x",
      article_date_display: null,
      datetime_notes: null,
      body_text: "watch the trailer",
      source_type: "youtube",
      source_name: "YT",
    }),
  );
  expect(line).toContain("#Трейлер");
  expect(line).toContain("#MarvelRivalsUA");
  expect(line).not.toContain("#Офіційно");
});

function leak(title, body, sourceType = "reddit") {
  return geminiDraftInput({
    title,
    article_url: "https://reddit.com/x",
    article_date_display: null,
    datetime_notes: null,
    body_text: body,
    source_type: sourceType,
    source_name: "Reddit (витоки Marvel Rivals)",
  });
}

it("always carries the rumour tag on a leak", () => {
  // Even when a topic tag matches, the marker stays — a label that shows up at
  // random is one readers cannot rely on.
  const line = publicHashtagLine(leak("New skin leaked", "a datamined costume for Psylocke"));

  expect(line).toContain("#Чутки");
  expect(line).toContain("#Скіни");
  expect(line).not.toContain("#Офіційно");
});

it("does not call a topic-less leak an announcement", () => {
  const line = publicHashtagLine(leak("Something odd in the files", "an unnamed asset showed up"));

  expect(line).toContain("#Чутки");
  expect(line).not.toContain("#Анонс");
});

it("tags rivalskins as a rumour too", () => {
  expect(publicHashtagLine(leak("Skin render", "upcoming render", "rivalskins"))).toContain("#Чутки");
});

it("keeps the stored rumour tags in step with the public marker", () => {
  const tags = fallbackTags(leak("New skin leaked", "a datamined costume"));

  expect(tags).toContain("чутки");
  expect(tags).toContain("marvelrivalsua");
});

it("keeps the announcement fallback for non-rumour sources", () => {
  // Bluesky/YouTube really do carry announcements, so their default is untouched.
  const line = publicHashtagLine(draft("bluesky"));

  expect(line).toContain("#Анонс");
  expect(line).not.toContain("#Чутки");
  expect(fallbackTags(draft("bluesky"))).not.toContain("чутки");
});

it("leaves official hashtags untouched by the rumour branch", () => {
  const line = publicHashtagLine(draft("official_marvel_rivals"));

  expect(line.startsWith("#MarvelRivalsUA #Офіційно")).toBe(true);
  expect(line).not.toContain("#Чутки");
});

it("gates the source link on both a source type and a URL", () => {
  expect(submissionAllowsSourceLink({ source_type: "official_marvel_rivals", source_url: "https://x" })).toBe(true);
  expect(submissionAllowsSourceLink({ source_type: "bluesky", source_url: "https://x" })).toBe(true);
  expect(submissionAllowsSourceLink({ source_type: "", source_url: "https://x" })).toBe(false); // user submission
  expect(submissionAllowsSourceLink({ source_type: "bluesky", source_url: "" })).toBe(false);
  expect(submissionAllowsSourceLink({})).toBe(false);
});

it("linkifies a generic source line", () => {
  const html = formatPostHtml("Текст новини.\n\nДжерело: MR Bluesky", {
    source_url: "https://bsky.app/profile/x",
    source_type: "wiki_facts",
    allow_source_link: true,
  });
  expect(html).toContain('<a href="https://bsky.app/profile/x">MR Bluesky</a>');
  expect(html).toContain("Джерело:");
});

it("does not link a generic source when disallowed", () => {
  const html = formatPostHtml("Джерело: MR Bluesky", {
    source_url: "https://bsky.app/profile/x",
    source_type: "wiki_facts",
    allow_source_link: false,
  });
  expect(html).not.toContain("<a");
  expect(html).toContain("Джерело: MR Bluesky");
});

it("does not link a generic source without a URL", () => {
  const html = formatPostHtml("Джерело: MR Bluesky", {
    source_url: "",
    source_type: "wiki_facts",
    allow_source_link: true,
  });
  expect(html).not.toContain("<a");
});

it("drops a source line from an ordinary collector post at render time", () => {
  // Covers drafts queued before the line became admin-only: they must not reach
  // the channel with it, and the moderation preview must show what will publish.
  const html = formatPostHtml("Текст новини.\n\nДжерело: YouTube Marvel Rivals\n\n#MarvelRivalsUA", {
    source_url: "https://youtube.com/watch?v=1",
    source_type: "youtube",
    allow_source_link: true,
  });

  expect(html).not.toContain("Джерело");
  expect(html).not.toContain("YouTube Marvel Rivals");
  expect(html).toContain("Текст новини.");
  expect(html).toContain("#MarvelRivalsUA");
  // The gap the removed line left is closed, not left as a double blank.
  expect(html).not.toContain("\n\n\n");
});

it("keeps a source line the wiki rubric is licensed to show", () => {
  const html = formatPostHtml("Чи знали ви, що…\n\nДжерело: Marvel Rivals Wiki (CC BY-SA)", {
    source_url: "https://marvelrivals.fandom.com/wiki/Loki",
    source_type: "wiki_facts",
    allow_source_link: true,
  });

  expect(html).toContain('<a href="https://marvelrivals.fandom.com/wiki/Loki">Marvel Rivals Wiki (CC BY-SA)</a>');
});

it("leaves a user submission's own text alone", () => {
  // No source type = a reader's submission; its wording is the author's, not ours.
  const html = formatPostHtml("Дивіться, що знайшов.\n\nДжерело: мій друг", {
    source_url: "",
    allow_source_link: false,
  });

  expect(html).toContain("Джерело: мій друг");
});

it("keeps an inline source mention that is not a label line", () => {
  const html = formatPostHtml("Гравці кажуть (джерело: форум), що патч близько.", {
    source_url: "https://reddit.com/x",
    source_type: "reddit",
    allow_source_link: true,
  });

  expect(html).toContain("джерело: форум");
});

it("still linkifies the official source", () => {
  const html = formatPostHtml(`Текст. ${OFFICIAL_SOURCE_ATTRIBUTION}`, {
    source_url: "https://www.marvelrivals.com/news/",
    source_type: "official_marvel_rivals",
    allow_source_link: true,
  });
  expect(html).toContain('<a href="https://www.marvelrivals.com/news/">офіційному сайті</a>');
});

it("plainly escapes text without a source line", () => {
  const html = formatPostHtml("Просто текст без джерела", {
    source_url: "https://x",
    allow_source_link: true,
  });
  expect(html).not.toContain("<a");
  expect(html).toContain("Просто текст без джерела");
});

it("selects the article prompt for the official source", () => {
  expect(selectPromptTemplate(draft("official_marvel_rivals"))).toContain("Текст статті:");
});

it("selects the short-form prompt for a non-official source", () => {
  const template = selectPromptTemplate(draft("bluesky"));
  expect(template.toLowerCase()).toContain("короткий допис");
  expect(template).not.toContain("Текст статті:");
  // The short-form post must be broken into paragraphs, not one solid block.
  expect(template.toLowerCase()).toContain("абзац");
});

it("gives a short-form draft no publication-date line", () => {
  // Non-official (social/short-form) posts must NOT carry a bare "2026-06-12"
  // publication-date line mid-post — only the body, the source attribution and
  // the hashtags. The date stays available to admins via original_text.
  const draftInput = geminiDraftInput({
    title: "Анонс",
    article_url: "https://bsky.app/profile/x/post/1",
    article_date_display: "2026-06-12",
    datetime_notes: null,
    body_text: "body",
    source_type: "bluesky",
    source_name: "MR Bluesky",
  });

  const result = ensureRequiredMetadata("Текст допису про подію.", draftInput);

  expect(result).not.toContain("2026-06-12");
  expect(result).not.toContain("Джерело");
  expect(result).toContain("#MarvelRivalsUA");
});

it("drops a source line the model added to a short-form draft anyway", () => {
  const result = ensureRequiredMetadata("Текст допису.\n\nДжерело: MR Bluesky", draft("bluesky", "MR Bluesky"));

  expect(result).not.toContain("Джерело");
  expect(result).toContain("Текст допису.");
});

it("adds the wiki credit to a fact draft that omitted it", () => {
  const result = ensureRequiredMetadata(
    "Чи знали ви, що Локі списаний з коміксу?",
    draft("wiki_facts", "Marvel Rivals Wiki (CC BY-SA)"),
  );

  expect(result).toContain("Джерело: Marvel Rivals Wiki (CC BY-SA)");
});

it("adds the rumour notice to a Reddit prompt", () => {
  const prompt = buildPrompt(draft("reddit", "Reddit (витоки Marvel Rivals)"));
  expect(prompt).toContain("НЕОФІЦІЙНИЙ");
  expect(prompt).toContain("датамайн");
  expect(prompt).not.toContain("{");
  expect(prompt).not.toContain("}");
});

it("adds no rumour notice to a non-rumour source", () => {
  const prompt = buildPrompt(draft("bluesky", "MR Bluesky"));
  expect(prompt).not.toContain("НЕОФІЦІЙНИЙ");
  expect(prompt).not.toContain("датамайн");
});

it("fills every placeholder in the short-form prompt", () => {
  const prompt = buildPrompt(draft("bluesky", "MR Bluesky"));
  expect(prompt).toContain("MR Bluesky");
  expect(prompt).not.toContain("{"); // every placeholder was filled
  expect(prompt).not.toContain("}");
});

it("tells the short-form prompt not to attribute at all", () => {
  const prompt = buildPrompt(draft("bluesky", "MR Bluesky"));

  expect(prompt).toContain("НЕ додавай атрибуцію");
  expect(prompt).not.toContain("Джерело: MR Bluesky");
});
