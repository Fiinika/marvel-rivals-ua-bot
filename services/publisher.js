import net from "node:net";

import { InputFile } from "grammy";

import { getLogger } from "./logger.js";
import { formatPostHtml, submissionAllowsSourceLink } from "./post_footer.js";
import { errorText, htmlUnescape, isDigits } from "./pyutils.js";
import { isTelegramBadRequest, isTelegramSendFailure, telegramErrorText } from "./telegram_errors.js";
import { delayBetweenTelegramSends, sendWithRetries } from "./telegram_retry.js";
import { urlsplit } from "./urlutils.js";
import { downloadYoutubeVideo } from "./youtube_video.js";

const logger = getLogger("services.publisher");

export const TELEGRAM_TEXT_LIMIT = 4096;
export const TELEGRAM_CAPTION_LIMIT = 1024;
export const DISABLED_LINK_PREVIEW = Object.freeze({ is_disabled: true });

// Sources whose posts are published as TEXT with a playable link preview of the
// source URL (e.g. YouTube — Telegram embeds the player) instead of a static photo.
export const PREVIEW_LINK_SOURCE_TYPES = new Set(["youtube"]);
// Sources whose "video" media is an EXTERNAL URL the bot must download and re-upload
// natively (Telegram can't fetch it by URL — e.g. a Bluesky getBlob MP4). User
// video submissions carry a Telegram file_id instead and are sent directly.
export const DOWNLOAD_VIDEO_SOURCE_TYPES = new Set(["bluesky"]);
// Album sources whose draft_text is ALREADY rendered HTML (built with its own
// hyperlinks/footer) and must be sent as-is; every other album caption is plain
// text run through the normal formatter.
export const PRERENDERED_ALBUM_SOURCE_TYPES = new Set(["reddit_fanart"]);

// Album images are downloaded and re-uploaded as bytes (not sent by URL): Telegram
// rejects by-URL photos over ~5MB (fan art is often larger) and a media group is
// atomic, so one bad URL fails the whole album. Uploading bytes lifts the limit to
// 10MB and lets us validate/skip non-image content first.
const ALBUM_IMAGE_USER_AGENT = "Mozilla/5.0 (compatible; MarvelRivalsUABot/1.0; +https://t.me/MarvelRivalsUABot)";
const ALBUM_IMAGE_TIMEOUT_SECONDS = 25.0;
const ALBUM_MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const ALBUM_ALLOWED_CONTENT_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

// Bot-side download of an external video (e.g. a Bluesky MP4 blob) for native
// re-upload. Same SSRF guards as the album downloader; only progressive MP4.
const EXTERNAL_VIDEO_TIMEOUT_SECONDS = 60.0;
const EXTERNAL_VIDEO_ALLOWED_CONTENT_TYPES = new Set(["video/mp4"]);

export class PublishingError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "PublishingError";
  }
}

export async function publishSubmission(bot, config, submission) {
  if (String(submission.message_type || "") === "album") {
    await publishAlbum(bot, config, submission);
    return;
  }

  const parseMode = "HTML";
  const forceSingleMediaMessage = false;
  const parts = submissionParts(submission);

  try {
    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index];
      const messageType = String(part.message_type || "text");
      const partSubmission = { ...submission, ...part, id: submission.id, message_type: messageType };
      const draftText = formatPartText(String(part.text || ""), partSubmission);

      if (messageType === "text" || messageType === "link") {
        await publishTextOrYoutubeVideo(bot, config, partSubmission, draftText, { parseMode });
      } else if (messageType === "photo") {
        await sendMediaWithOptionalText(bot, sendPhoto(bot), config.publish_chat_id, partSubmission, draftText, {
          parseMode,
          forceSingleMessage: forceSingleMediaMessage,
        });
      } else if (messageType === "video" && needsExternalVideoDownload(partSubmission)) {
        await sendDownloadedVideoPost(bot, config.publish_chat_id, String(partSubmission.media_url || ""), draftText, {
          parseMode,
          linkPreview: linkPreviewOptionsFor(partSubmission),
          maxBytes: Math.max(1, config.bluesky_video_max_mb) * 1024 * 1024,
        });
      } else if (messageType === "video") {
        await sendMediaWithOptionalText(bot, sendVideo(bot), config.publish_chat_id, partSubmission, draftText, {
          parseMode,
          forceSingleMessage: forceSingleMediaMessage,
        });
      } else if (messageType === "document") {
        await sendMediaWithOptionalText(bot, sendDocument(bot), config.publish_chat_id, partSubmission, draftText, {
          parseMode,
          forceSingleMessage: forceSingleMediaMessage,
        });
      } else {
        throw new PublishingError(`Unsupported submission type: ${messageType}`);
      }
      if (index < parts.length - 1) {
        await delayBetweenTelegramSends();
      }
    }
  } catch (error) {
    if (error instanceof PublishingError) {
      throw error;
    }
    if (!isTelegramSendFailure(error)) {
      throw error;
    }
    throw new PublishingError("Telegram API rejected the publish request", { cause: error });
  }

  logger.info(`Published submission ${submission.id} to chat ${config.publish_chat_id}`);
}

async function publishAlbum(bot, config, submission) {
  const images = albumImageUrls(submission);
  const caption = albumCaptionHtml(submission);
  try {
    await sendAlbumMessage(bot, config.publish_chat_id, images, caption);
  } catch (error) {
    if (error instanceof PublishingError) {
      throw error;
    }
    if (!isTelegramSendFailure(error)) {
      throw error;
    }
    throw new PublishingError("Telegram API rejected the album publish request", { cause: error });
  }

  logger.info(`Published album submission ${submission.id} to chat ${config.publish_chat_id}`);
}

async function publishTextOrYoutubeVideo(bot, config, submission, text, { parseMode }) {
  const linkPreview = linkPreviewOptionsFor(submission);
  const sourceType = String(submission.source_type || "");
  const sourceUrl = String(submission.source_url || "").trim();
  if (config.enable_youtube_video_download && PREVIEW_LINK_SOURCE_TYPES.has(sourceType) && isSafeHttpUrl(sourceUrl)) {
    await sendYoutubePost(bot, config.publish_chat_id, sourceUrl, text, {
      parseMode,
      linkPreview,
      maxBytes: Math.max(1, config.youtube_video_max_mb) * 1024 * 1024,
    });
    return;
  }

  await sendText(bot, config.publish_chat_id, text, { parseMode, linkPreview });
}

/**
 * Whether a video part is an external URL the bot must download + re-upload
 * (a DOWNLOAD_VIDEO_SOURCE_TYPES source with an https media_url and no file_id),
 * rather than a Telegram file_id that sendVideo can take directly.
 */
export function needsExternalVideoDownload(part) {
  if (String(part.message_type || "") !== "video") {
    return false;
  }
  if (!DOWNLOAD_VIDEO_SOURCE_TYPES.has(String(part.source_type || ""))) {
    return false;
  }
  if (String(part.file_id || "").trim()) {
    return false;
  }
  const mediaUrl = String(part.media_url || "");
  // https-only, matching what downloadExternalVideo can actually fetch.
  return isSafeHttpUrl(mediaUrl) && urlsplit(mediaUrl).scheme === "https";
}

/**
 * Download an external MP4 for native re-upload. SSRF-guarded (https, no
 * internal-IP hosts, no redirects) and streamed with a hard size cap; returns
 * null when the URL is disallowed, the body is not an MP4, it is too large, or
 * the download fails.
 */
export async function downloadExternalVideo(url, { maxBytes }) {
  if (!isFetchableMediaUrl(url)) {
    logger.warning(`Skipping external video with a disallowed URL: ${url}`);
    return null;
  }

  let buffer;
  try {
    const response = await fetch(url, {
      redirect: "manual",
      headers: { "User-Agent": ALBUM_IMAGE_USER_AGENT },
      signal: AbortSignal.timeout(EXTERNAL_VIDEO_TIMEOUT_SECONDS * 1000),
    });
    if (response.status >= 400) {
      throw new Error(`HTTP ${response.status}`);
    }

    const contentType = String(response.headers.get("content-type") ?? "")
      .split(";")[0]
      .trim()
      .toLowerCase();
    if (!EXTERNAL_VIDEO_ALLOWED_CONTENT_TYPES.has(contentType)) {
      logger.warning(`Skipping external video with content-type ${JSON.stringify(contentType)}: ${url}`);
      return null;
    }

    const declared = String(response.headers.get("content-length") ?? "");
    if (isDigits(declared) && Number(declared) > maxBytes) {
      logger.info(`External video too large (declared ${declared} bytes); skipping: ${url}`);
      return null;
    }

    buffer = await readCappedBody(response, maxBytes);
    if (buffer === null) {
      logger.info(`External video over the ${maxBytes}-byte limit; skipping: ${url}`);
      return null;
    }
  } catch (error) {
    logger.warning(`External video download failed for ${url}: ${errorText(error)}`);
    return null;
  }

  if (!buffer.length) {
    return null;
  }
  return { data: buffer, filename: "video.mp4" };
}

/**
 * Send an external-URL video as a NATIVE inline Telegram video (downloaded +
 * re-uploaded), falling back to a text post (with the source link) when the
 * video is unavailable, too large, or the upload is rejected. Returns the sent
 * message.
 */
export async function sendDownloadedVideoPost(bot, chatId, videoUrl, caption, { parseMode, linkPreview, maxBytes }) {
  const video = await downloadExternalVideo(videoUrl, { maxBytes });
  if (video !== null) {
    try {
      return await sendVideoBytes(bot, chatId, video.data, video.filename, caption, { parseMode });
    } catch (error) {
      if (!isTelegramSendFailure(error)) {
        throw error;
      }
      logger.warning(`Native external video upload failed (${errorText(error)}); falling back to text.`);
    }
  }

  return sendText(bot, chatId, caption, { parseMode, linkPreview });
}

/**
 * Send a YouTube post as a NATIVE inline video (downloaded + re-uploaded),
 * falling back to a text post with a playable link preview when the video is
 * unavailable, too large, or the upload is rejected. Returns the primary sent
 * message so callers (moderation) can record it.
 */
export async function sendYoutubePost(bot, chatId, sourceUrl, caption, { parseMode, linkPreview, maxBytes }) {
  const video = await downloadYoutubeVideo(sourceUrl, { maxBytes });
  if (video !== null) {
    try {
      return await sendVideoBytes(bot, chatId, video.data, video.filename, caption, { parseMode });
    } catch (error) {
      if (!isTelegramSendFailure(error)) {
        throw error;
      }
      logger.warning(`Native YouTube video upload failed (${errorText(error)}); falling back to link preview.`);
    }
  }

  return sendText(bot, chatId, caption, { parseMode, linkPreview });
}

async function sendVideoBytes(bot, chatId, data, filename, caption, { parseMode }) {
  const trimmedCaption = caption.trim();
  const captionFits =
    Boolean(trimmedCaption) && telegramVisibleLength(trimmedCaption, parseMode) <= TELEGRAM_CAPTION_LIMIT;

  if (captionFits) {
    return sendWithRetries(
      (signal) =>
        bot.api.sendVideo(
          chatId,
          new InputFile(data, filename),
          { caption: trimmedCaption || undefined, parse_mode: parseMode, supports_streaming: true },
          signal,
        ),
      { label: "publish youtube video" },
    );
  }

  const videoMessage = await sendWithRetries(
    (signal) =>
      bot.api.sendVideo(chatId, new InputFile(data, filename), { supports_streaming: true }, signal),
    { label: "publish youtube video" },
  );
  if (trimmedCaption) {
    await delayBetweenTelegramSends();
    await sendText(bot, chatId, trimmedCaption, { parseMode });
  }
  return videoMessage;
}

/**
 * Enable a large, playable link preview of the source URL for sources in
 * PREVIEW_LINK_SOURCE_TYPES (YouTube); disable previews for everything else.
 */
export function linkPreviewOptionsFor(submission) {
  const sourceType = String(submission.source_type || "");
  const sourceUrl = String(submission.source_url || "").trim();
  if (PREVIEW_LINK_SOURCE_TYPES.has(sourceType) && isSafeHttpUrl(sourceUrl)) {
    return { is_disabled: false, url: sourceUrl, prefer_large_media: true, show_above_text: true };
  }
  return DISABLED_LINK_PREVIEW;
}

/**
 * The album caption as HTML. Digest captions are pre-rendered HTML (sent as-is);
 * collector-album captions are plain Gemini text run through the post formatter.
 */
export function albumCaptionHtml(submission) {
  const text = String(submission.draft_text || "").trim();
  if (PRERENDERED_ALBUM_SOURCE_TYPES.has(String(submission.source_type || ""))) {
    return text;
  }
  return formatPartText(text, submission);
}

function isSafeHttpUrl(value) {
  const parsed = urlsplit(String(value).trim());
  return (parsed.scheme === "http" || parsed.scheme === "https") && Boolean(parsed.netloc);
}

/**
 * The de-duplicated image URLs of an `album` submission (one per part), capped
 * at Telegram's media-group maximum of 10.
 */
export function albumImageUrls(submission) {
  const urls = [];
  const seen = new Set();
  for (const part of submission.parts ?? []) {
    const url = String(part.media_url || "").trim();
    if (url && !seen.has(url)) {
      seen.add(url);
      urls.push(url);
    }
  }
  if (!urls.length) {
    const single = String(submission.media_url || "").trim();
    if (single) {
      urls.push(single);
    }
  }
  return urls.slice(0, 10);
}

/**
 * Send an image album to `chatId`. Images are downloaded and uploaded as bytes
 * (unreachable / non-image / oversized ones are skipped). A media group needs
 * 2-10 items, so a single usable image is sent as a plain photo. The caption
 * rides on the first item when it fits Telegram's 1024 limit, otherwise it
 * follows as a separate text message.
 */
export async function sendAlbumMessage(bot, chatId, images, caption, { parseMode = "HTML" } = {}) {
  if (!images.length) {
    throw new PublishingError("Album submission has no images");
  }

  const photos = await downloadAlbumPhotos(images);
  if (!photos.length) {
    throw new PublishingError("None of the album images could be downloaded as valid photos");
  }

  const trimmedCaption = caption.trim();
  const captionFits =
    Boolean(trimmedCaption) && telegramVisibleLength(trimmedCaption, parseMode) <= TELEGRAM_CAPTION_LIMIT;

  // A media group is atomic: if Telegram rejects one image (e.g. invalid
  // dimensions, which the size/content-type guard can't catch), the whole send
  // fails. So drop the offending item (Telegram names it as "message #N") and
  // retry, rather than losing the entire album.
  const remaining = [...photos];
  while (remaining.length) {
    try {
      if (remaining.length === 1) {
        const [data, filename] = remaining[0];
        await sendWithRetries(
          (signal) =>
            bot.api.sendPhoto(
              chatId,
              new InputFile(data, filename),
              captionFits ? { caption: trimmedCaption, parse_mode: parseMode } : {},
              signal,
            ),
          { label: "publish album single photo" },
        );
      } else {
        await sendWithRetries(
          (signal) =>
            bot.api.sendMediaGroup(
              chatId,
              buildAlbumMedia(remaining, trimmedCaption, { captionFits, parseMode }),
              {},
              signal,
            ),
          { label: "publish album media group" },
        );
      }
      break;
    } catch (error) {
      if (!isTelegramBadRequest(error)) {
        throw error;
      }
      const index = failingMediaIndex(telegramErrorText(error), remaining.length);
      const [dropped] = remaining.splice(index, 1);
      logger.warning(
        `Telegram rejected album image ${dropped[1]} (${errorText(error)}); ` +
          `dropping it and retrying with ${remaining.length} left.`,
      );
      if (!remaining.length) {
        throw new PublishingError("Every album image was rejected by Telegram", { cause: error });
      }
    }
  }

  if (trimmedCaption && !captionFits) {
    await delayBetweenTelegramSends();
    await sendText(bot, chatId, trimmedCaption, { parseMode });
  }
}

function buildAlbumMedia(photos, caption, { captionFits, parseMode }) {
  return photos.map(([data, filename], index) => {
    const file = new InputFile(data, filename);
    if (index === 0 && captionFits) {
      return { type: "photo", media: file, caption, parse_mode: parseMode };
    }
    return { type: "photo", media: file };
  });
}

/**
 * The 0-based index Telegram blamed in a media-group error ("...message #N..."),
 * or 0 when the message names no usable index.
 */
export function failingMediaIndex(errorMessage, count) {
  const match = /message #(\d+)/.exec(errorMessage);
  if (match) {
    const index = Number(match[1]) - 1;
    if (index >= 0 && index < count) {
      return index;
    }
  }
  return 0;
}

/**
 * Guard the bot-side album download against SSRF. Require https, and reject an
 * IP-literal host in a non-public range (loopback / private / link-local /
 * reserved) — e.g. the cloud metadata endpoint 169.254.169.254 or 127.0.0.1.
 * Hostnames pass through: redirects are disabled on the download client and the
 * collectors already allowlist their image CDN host, so the only thing left to
 * block here is an explicit internal-IP target.
 */
export function isFetchableMediaUrl(url) {
  const parsed = urlsplit(String(url).trim());
  if (parsed.scheme !== "https") {
    return false;
  }
  const host = parsed.hostname;
  if (!host) {
    return false;
  }
  const ipVersion = net.isIP(host);
  if (ipVersion === 0) {
    return true; // a DNS hostname, not an IP literal
  }
  return isGlobalIp(host, ipVersion);
}

/**
 * Python's `ipaddress.ip_address(host).is_global`, for the ranges an attacker
 * could actually aim at a server: loopback, private, link-local, shared address
 * space, multicast and the reserved blocks.
 */
function isGlobalIp(host, version) {
  if (version === 4) {
    const octets = host.split(".").map(Number);
    const [a, b] = octets;
    if (a === 0 || a === 10 || a === 127) return false;
    if (a === 100 && b >= 64 && b <= 127) return false; // 100.64.0.0/10 shared
    if (a === 169 && b === 254) return false; // link-local
    if (a === 172 && b >= 16 && b <= 31) return false;
    if (a === 192 && b === 0) return false; // 192.0.0.0/24 + 192.0.2.0/24
    if (a === 192 && b === 168) return false;
    if (a === 198 && (b === 18 || b === 19)) return false; // benchmarking
    if (a === 198 && b === 51) return false; // TEST-NET-2
    if (a === 203 && b === 0) return false; // TEST-NET-3
    if (a >= 224) return false; // multicast + reserved + broadcast
    return true;
  }

  const lowered = host.toLowerCase();
  if (lowered === "::" || lowered === "::1") return false;
  if (lowered.startsWith("fe8") || lowered.startsWith("fe9") || lowered.startsWith("fea") || lowered.startsWith("feb")) {
    return false; // link-local fe80::/10
  }
  if (lowered.startsWith("fc") || lowered.startsWith("fd")) return false; // unique local
  if (lowered.startsWith("ff")) return false; // multicast
  if (lowered.startsWith("::ffff:")) {
    const mapped = lowered.slice("::ffff:".length);
    return net.isIP(mapped) === 4 ? isGlobalIp(mapped, 4) : false;
  }
  return true;
}

async function downloadAlbumPhotos(images) {
  const photos = [];
  // redirect "manual" so an allowlisted-looking URL cannot 302 to an internal
  // host — the redirect target would otherwise be an unchecked SSRF target.
  for (let index = 0; index < images.length; index += 1) {
    const photo = await downloadAlbumPhoto(images[index], index);
    if (photo !== null) {
      photos.push(photo);
    }
  }
  return photos;
}

/**
 * Download and validate one album image.
 *
 * `maxBytes` is a parameter only so the size guard can be exercised in tests
 * without pushing ten megabytes through the stub; production always uses the cap.
 */
export async function downloadAlbumPhoto(url, index, { maxBytes = ALBUM_MAX_IMAGE_BYTES } = {}) {
  if (!isFetchableMediaUrl(url)) {
    logger.warning(`Skipping album image with a disallowed URL: ${url}`);
    return null;
  }

  let contentType;
  let buffer;
  try {
    // Stream so an oversized body is aborted mid-download instead of being fully
    // buffered into memory first (a non-image internal/huge response otherwise
    // costs up to its full size in RAM before the post-hoc length check).
    const response = await fetch(url, {
      redirect: "manual",
      headers: { "User-Agent": ALBUM_IMAGE_USER_AGENT },
      signal: AbortSignal.timeout(ALBUM_IMAGE_TIMEOUT_SECONDS * 1000),
    });
    if (response.status >= 400) {
      throw new Error(`HTTP ${response.status}`);
    }

    contentType = String(response.headers.get("content-type") ?? "")
      .split(";")[0]
      .trim()
      .toLowerCase();
    if (!ALBUM_ALLOWED_CONTENT_TYPES.has(contentType)) {
      logger.warning(`Skipping album image with unsupported content-type ${JSON.stringify(contentType)}: ${url}`);
      return null;
    }

    const declared = String(response.headers.get("content-length") ?? "");
    if (isDigits(declared) && Number(declared) > maxBytes) {
      logger.warning(`Skipping album image (declared ${declared} bytes) over the upload limit: ${url}`);
      return null;
    }

    buffer = await readCappedBody(response, maxBytes);
    if (buffer === null) {
      logger.warning(`Skipping album image over the ${maxBytes}-byte upload limit: ${url}`);
      return null;
    }
  } catch (error) {
    logger.warning(`Album image download failed for ${url}: ${errorText(error)}`);
    return null;
  }

  if (!buffer.length) {
    logger.warning(`Skipping empty album image: ${url}`);
    return null;
  }

  const extension = contentType === "image/png" ? "png" : contentType === "image/webp" ? "webp" : "jpg";
  return [buffer, `art_${index + 1}.${extension}`];
}

/** Read a response body, giving up (null) the moment it exceeds `maxBytes`. */
async function readCappedBody(response, maxBytes) {
  const chunks = [];
  let total = 0;
  if (!response.body) {
    return Buffer.alloc(0);
  }
  for await (const chunk of response.body) {
    const part = Buffer.from(chunk);
    total += part.length;
    if (total > maxBytes) {
      await response.body.cancel?.().catch(() => {});
      return null;
    }
    chunks.push(part);
  }
  return Buffer.concat(chunks, total);
}

function submissionParts(submission) {
  const parts = submission.parts;
  if (Array.isArray(parts) && parts.length) {
    return parts.map((part) => ({ ...part }));
  }

  return [
    {
      message_type: submission.message_type,
      text: submission.draft_text || submission.original_text || "",
      file_id: submission.file_id,
      media_url: submission.media_url,
    },
  ];
}

function formatPartText(text, submission) {
  return formatPostHtml(text, {
    source_url: String(submission.source_url || ""),
    allow_source_link: submissionAllowsSourceLink(submission),
    include_community_footer: true,
  });
}

async function sendText(bot, chatId, text, { parseMode = null, linkPreview = DISABLED_LINK_PREVIEW } = {}) {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new PublishingError("Text submission has no draft text");
  }

  if (parseMode !== null) {
    if (telegramVisibleLength(trimmed, parseMode) > TELEGRAM_TEXT_LIMIT) {
      throw new PublishingError("Text submission exceeds Telegram single-message limit");
    }
    return sendWithRetries(
      (signal) =>
        bot.api.sendMessage(
          chatId,
          trimmed,
          { parse_mode: parseMode, link_preview_options: linkPreview },
          signal,
        ),
      { label: "publish text message" },
    );
  }

  const chunks = splitText(trimmed, TELEGRAM_TEXT_LIMIT);
  for (let index = 0; index < chunks.length; index += 1) {
    await sendWithRetries(
      (signal) => bot.api.sendMessage(chatId, chunks[index], { link_preview_options: linkPreview }, signal),
      { label: "publish text chunk" },
    );
    if (index < chunks.length - 1) {
      await delayBetweenTelegramSends();
    }
  }
  return undefined;
}

/** The three media senders, as (chatId, media, other, signal) callbacks. */
export const sendPhoto = (bot) => (chatId, media, other, signal) => bot.api.sendPhoto(chatId, media, other, signal);
export const sendVideo = (bot) => (chatId, media, other, signal) => bot.api.sendVideo(chatId, media, other, signal);
export const sendDocument = (bot) => (chatId, media, other, signal) =>
  bot.api.sendDocument(chatId, media, other, signal);

async function sendMediaWithOptionalText(
  bot,
  sendMedia,
  chatId,
  submission,
  text,
  { parseMode = null, forceSingleMessage = false } = {},
) {
  const mediaValue = submission.file_id || submission.media_url;
  if (!mediaValue) {
    throw new PublishingError("Media submission has no file_id or media_url");
  }

  const usesExternalMedia = !submission.file_id && Boolean(submission.media_url);
  const trimmed = text.trim();

  try {
    if (trimmed && telegramVisibleLength(trimmed, parseMode) <= TELEGRAM_CAPTION_LIMIT) {
      await sendWithRetries((signal) => sendMedia(chatId, mediaValue, { caption: trimmed, parse_mode: parseMode }, signal), {
        label: `publish ${submission.message_type} with caption`,
      });
      return;
    }

    if (forceSingleMessage) {
      throw new PublishingError("Media caption exceeds Telegram single-message limit");
    }

    await sendWithRetries((signal) => sendMedia(chatId, mediaValue, { parse_mode: parseMode }, signal), {
      label: `publish ${submission.message_type} without long caption`,
    });
  } catch (error) {
    if (error instanceof PublishingError) {
      throw error;
    }
    if (!isTelegramSendFailure(error)) {
      throw error;
    }
    if (!usesExternalMedia) {
      throw error;
    }

    logger.exception(
      `Failed to publish external media for submission ${submission.id}. Falling back to text-only publish.`,
      error,
    );
    await sendText(bot, chatId, trimmed, { parseMode });
    return;
  }

  if (trimmed) {
    await delayBetweenTelegramSends();
    await sendText(bot, chatId, trimmed, { parseMode });
  }
}

export function splitText(text, limit) {
  if (text.length <= limit) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > limit) {
    // Python's `rfind(x, 0, limit)` searches indices < limit.
    let splitAt = remaining.lastIndexOf("\n", limit - 1);
    if (splitAt < Math.floor(limit / 2)) {
      splitAt = limit;
    }
    chunks.push(remaining.slice(0, splitAt).trim());
    remaining = remaining.slice(splitAt).trim();
  }

  if (remaining) {
    chunks.push(remaining);
  }

  return chunks;
}

export function telegramVisibleLength(text, parseMode) {
  if (parseMode !== "HTML") {
    return text.length;
  }

  const withoutTags = text.replace(/<[^>]+>/g, "");
  return htmlUnescape(withoutTags).length;
}
