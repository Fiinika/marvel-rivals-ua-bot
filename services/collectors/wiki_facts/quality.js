/**
 * Which Trivia bullets are worth publishing, and in what order.
 *
 * The rubric posts ONE fact a week, so the fact has to carry the whole post: it
 * must read as a standalone sentence (the reader never sees the bullet above it)
 * and it should be the kind of thing worth telling someone. Everything here is a
 * judgement about the ENGLISH source text; the Ukrainian rewrite is Gemini's job.
 *
 * The rules are deliberately split in two:
 *
 *  * `rejectReason` — hard structural disqualifiers. A bullet that fails one can
 *    never be fixed by better prompting, because the information it needs is in
 *    a sibling bullet we are not publishing.
 *  * `tierOf` — a soft preference among the survivors. Nothing is dropped here;
 *    a lower tier only means "pick something else first", so a hero whose whole
 *    Trivia section is debut trivia still gets a post.
 */

/** A bullet that only introduces the nested list under it is not a fact. */
const LIST_HEADER_RE = /[:;]\s*$/;

/**
 * Openers whose referent lives in the PREVIOUS bullet ("This song…", "Another
 * statue…"). The hero's own pronouns (he/she/his/her/their) are deliberately
 * NOT here: the post title names the hero, so the prompt can resolve those.
 */
const DANGLING_OPENER_RE =
  /^(?:this|that|these|those|it|they|them|such|both|either|neither|another|additionally|furthermore|moreover|similarly|likewise|besides|conversely|meanwhile|the (?:rework|change|update|song|joke|latter|former|two|others?)\b|in (?:addition|contrast|this|the same)|on a similar|as a result|because of this|due to this|despite this|apart from|aside from|upon this)\b/i;

/** "There is a secret voice line…" is existential, not a back-reference. */
const EXISTENTIAL_THERE_RE = /^there\s+(?:is|are|was|were|has|have)\b/i;
const DEMONSTRATIVE_RE = /^(?:this|that|these|those)\b/i;

/** The formulaic "<hero>'s first appearance was in <comic> (<year>) #N in <Month Year>". */
const DEBUT_OPENER_RE =
  /^.{0,60}?\bfirst(?: full| full-length| comics?| official)? appear(?:ed|ance|s)\b|^.{0,40}?\b(?:originally |technically |later )?debuted\b/i;
/** …which is only the FORMULA when it carries the comic citation that makes it one. */
const COMIC_CITATION_RE = /\(\d{4}\)\s*#\d+|\bin\s+[A-Z][a-z]+\s+\d{4}\b/;

/** What the rubric exists to publish: cast, easter eggs, in-game flavour, lore. */
const INTERESTING_RES = [
  /\b(?:voiced by|voice actor|voice actress|reprising (?:his|her|their) role|is voiced|provides? the voice|created by|co-created|named (?:him|her|it|them)? ?after)\b/i,
  /\b(?:reference[sd]?|referencing|homage|easter egg|nod to|inspired by|based (?:on|off)|parody|pays? tribute|call ?back)\b/i,
  /\b(?:voice ?line|interaction|emote|spray|MVP animation|achievement|gallery card|quote[sd]?|says|will say|can be (?:seen|found|heard)|statue|billboard|poster|reads)\b/i,
  /\b(?:the (?:first|only|last|oldest|youngest|fastest|slowest|shortest|tallest|most|least|highest|lowest)|is the \w+ (?:hero|character|duelist|vanguard|strategist))\b/i,
  /\b(?:in the comics|comic book origin|canon|lore|mythology|storyline|retcon)\b/i,
  /\b(?:meme|film|movie|band|album|song|series|anime|manga|actor|singer|composer|Netflix|MCU)\b/i,
];

/** Balance-patch and datamine bookkeeping: true, but nobody wants it on a Sunday. */
const DULL_RES = [
  /\b(?:datamined|patch|version \d|buffed|nerfed|hitbox|cooldown|win ?rate|pick ?rate|tier list)\b/i,
  /\bTop 500\b/,
];

/** Lower sorts first. */
export const FactTier = Object.freeze({ INTERESTING: 0, PLAIN: 1, NESTED: 2, FORMULAIC: 3 });

/**
 * Why this bullet can never stand on its own as a post, or null when it can.
 *
 * `depth` is the wikitext nesting level (1 = top level). A nested bullet is a
 * continuation of the one above it — "Another huge statue inside the lobby." —
 * and is usually meaningless without its parent, which we are not publishing.
 * A third of them do name their own subject and end as a full sentence though
 * ("Blade's Thousand-Fold Slash is likely similar to Vergil's Rapid Slash."), so
 * those are kept and demoted to FactTier.NESTED rather than thrown away.
 */
export function rejectReason(fact) {
  const text = String(fact.fact ?? "");
  // A fake/legacy fact without depth is treated as top level.
  if ((fact.depth ?? 1) > 1 && !isSelfContained(fact)) {
    return "nested";
  }
  if (LIST_HEADER_RE.test(text)) {
    return "list_header";
  }
  if (EXISTENTIAL_THERE_RE.test(text)) {
    return null;
  }
  if (DANGLING_OPENER_RE.test(text)) {
    // "This version of Magneto…" names its own subject and reads fine alone.
    if (DEMONSTRATIVE_RE.test(text) && namesHero(text.slice(0, 60), fact.hero)) {
      return null;
    }
    return "dangling_opener";
  }
  return null;
}

/**
 * A nested bullet that carries its own subject: it names the hero somewhere and
 * ends as a finished sentence. That rules out the enumeration fragments ("Ace of
 * Hearts - Him and Rogue", "Another huge statue inside the lobby.") while keeping
 * the ones that only happen to be indented.
 */
function isSelfContained(fact) {
  const text = String(fact.fact ?? "");
  return /[.!?]["']?$/.test(text) && namesHero(text, fact.hero);
}

/** Does this text contain a word from the hero's page title? */
function namesHero(text, hero) {
  const lower = text.toLowerCase();
  return String(hero ?? "")
    .split(/[^A-Za-z]+/)
    .filter((word) => word.length > 2 && !["the", "and"].includes(word.toLowerCase()))
    .some((word) => lower.includes(word.toLowerCase()));
}

/** The soft preference among bullets that passed `rejectReason`. */
export function tierOf(fact) {
  const text = String(fact.fact ?? "");
  if (DEBUT_OPENER_RE.test(text) && COMIC_CITATION_RE.test(text)) {
    return FactTier.FORMULAIC;
  }
  // Kept, but behind every top-level bullet: it survived the nesting check on a
  // heuristic, so it is the likeliest place for a missed back-reference.
  if ((fact.depth ?? 1) > 1) {
    return FactTier.NESTED;
  }
  if (DULL_RES.some((re) => re.test(text))) {
    return FactTier.PLAIN;
  }
  return INTERESTING_RES.some((re) => re.test(text)) ? FactTier.INTERESTING : FactTier.PLAIN;
}
