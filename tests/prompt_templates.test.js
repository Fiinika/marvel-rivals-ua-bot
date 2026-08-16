/**
 * Every prompt file must render with the exact keys its caller passes.
 *
 * The prompts go through `formatTemplate`, which reproduces Python's
 * `str.format`: `{` and `}` are structural, and a literal brace has to be
 * doubled. A JSON example written as {"pick": 1} instead of {{"pick": 1}} is
 * therefore read as a replacement field named `"pick": 1`, and rendering throws
 * "Missing format key" every single time.
 *
 * That failure is close to invisible in production — the caller catches it and
 * falls back — so the feature would be silently dead rather than broken. These
 * tests render each template exactly the way the code does, which is the only
 * check that would have caught it.
 */

import fs from "node:fs";

import { describe, expect, it } from "vitest";

import {
  DEDUP_PROMPT_PATH,
  PROMPT_PATH,
  SHORT_FORM_PROMPT_PATH,
  STYLE_PROMPT_PATH,
  WIKI_FACT_PROMPT_PATH,
  WIKI_PICK_PROMPT_PATH,
} from "../services/gemini.js";
import { formatTemplate } from "../services/pyutils.js";

// The key sets buildPrompt / buildDedupPrompt / pickBestTriviaFact actually pass.
const DRAFT_KEYS = {
  style_prompt: "s",
  source_type: "s",
  source_name: "s",
  title: "s",
  article_url: "s",
  article_type_label: "s",
  date_line: "s",
  source_line: "s",
  hashtag_line: "s",
  datetime_notes: "s",
  body_text: "s",
  rumor_notice: "s",
};

const TEMPLATES = [
  ["gemini_news_uk.md", PROMPT_PATH, DRAFT_KEYS],
  ["gemini_shortform_uk.md", SHORT_FORM_PROMPT_PATH, DRAFT_KEYS],
  ["gemini_wiki_fact_uk.md", WIKI_FACT_PROMPT_PATH, DRAFT_KEYS],
  ["gemini_dedup_uk.md", DEDUP_PROMPT_PATH, { new_title: "t", existing_titles: "1. a" }],
  ["gemini_wiki_pick_uk.md", WIKI_PICK_PROMPT_PATH, { facts: "1. a\n2. b" }],
];

// Python's text mode normalised these; loadCachedText now does the same.
const read = (file) => fs.readFileSync(file, "utf8").replaceAll("\r\n", "\n").trim();

describe.each(TEMPLATES)("%s", (name, file, keys) => {
  it("renders with the keys its caller passes", () => {
    expect(() => formatTemplate(read(file), keys)).not.toThrow();
  });

  it("leaves no unreplaced placeholder behind", () => {
    const rendered = formatTemplate(read(file), keys);
    // A surviving single brace means a literal brace was not doubled.
    expect(rendered).not.toMatch(/\{[a-z_][a-z0-9_]*\}/);
  });
});

it("keeps the JSON examples escaped so they survive rendering", () => {
  // The two prompts that ask for a JSON reply must still SHOW real JSON braces
  // to the model after rendering - that is the whole point of doubling them.
  expect(formatTemplate(read(DEDUP_PROMPT_PATH), { new_title: "t", existing_titles: "1. a" })).toContain(
    '{"duplicate":',
  );
  expect(formatTemplate(read(WIKI_PICK_PROMPT_PATH), { facts: "1. a" })).toContain('{"pick":');
});

it("has no style prompt placeholder left unfilled", () => {
  // The style prompt is inlined into the news prompt, not formatted itself.
  expect(read(STYLE_PROMPT_PATH)).not.toContain("{");
});
