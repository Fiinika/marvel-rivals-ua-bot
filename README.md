# Marvel Rivals UA Submission Bot

MVP Telegram bot for a Ukrainian Marvel Rivals community. Users send text, links, photos, videos, or documents to the bot. The bot stores each submission in SQLite, sends it to a private admin moderation chat, and lets approved admins publish accepted submissions to a public Telegram channel or group.

## What It Does

- Accepts manual user submissions:
  - plain text
  - links
  - photos with optional captions
  - videos with optional captions
  - documents with optional captions
- Stores submissions in SQLite with `pending`, `published`, or `rejected` status.
- Sends the proposed post part or parts to the moderation chat first, then a separate metadata/control message with inline buttons.
- Allows only configured admin user IDs to approve, reject, or edit.
- Publishes approved text or original media to the configured public chat.
- Handles manual long media captions by publishing the media first, then the draft text as a separate message. Official-news drafts are intentionally short to avoid moderation spam, and media captions fall back to a separate text draft when needed.
- Supports part-based editing, adding new post parts, and `/cancel` for active edit prompts.
- Limits user submissions with configurable per-user cooldown.
- Fetches the official Marvel Rivals news page and creates Ukrainian Gemini drafts for new articles.
- Sends every AI-generated official news draft to the same admin moderation queue as manual submissions.
- Tracks already moderated official articles in SQLite to avoid duplicate drafts.
- Supports official news cover images/photos when safe media URLs are detected, with extra images reserved for relevant later parts instead of being spammed.

## What It Does Not Do Yet

The bot still keeps publication fully manual. It does not implement:

- Reddit parsing
- auto-posting
- gaming media source parsing
- video parsing for official news posts

The code is split into handlers and services so future sources can be added without creating a separate moderation flow.

## Project Structure

```text
main.py
config.py
database.py
discord_moderation.py
discord_badwords.txt
keyboards.py
handlers/
  user.py
  admin.py
services/
  collectors/
    base.py
    registry.py
    official_marvel_rivals/
      article_parser.py
      collector.py
      news_fetcher.py
  date_utils.py
  i18n.py
  formatter.py
  gemini.py
  media_parser.py
  moderation.py
  post_footer.py
  publisher.py
prompts/
  gemini_news_uk.md
  official_news_style.md
locales/
  uk.json
README.md
requirements.txt
.env.example
.gitignore
```

## Create a Bot With BotFather

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`.
3. Follow the prompts for bot name and username.
4. BotFather will return a token. Use it as `BOT_TOKEN`.

Keep the token private. Anyone with the token can control the bot.

## Get `BOT_TOKEN`

Use the token from BotFather:

```env
BOT_TOKEN=1234567890:your_real_token_here
```

## Get `ADMIN_CHAT_ID`

`ADMIN_CHAT_ID` is the private moderation chat where submissions and buttons are sent.

A private group or supergroup is recommended. A Telegram channel can be used for moderation previews and buttons, but channel posts do not expose the real admin user ID to the bot.

In channel-based moderation, the admin user ID is checked when inline buttons are clicked. Existing parts are edited through copied draft posts with `💾 Зберегти`; new parts are added by clicking `➕ Нова частина` and sending the next text, photo, video, or document post in the admin channel.

Telegram can show modal alerts only after inline button clicks, not after ordinary text messages. After a new part is received, the confirmation is the metadata/control message moving back to the bottom.

Recommended method:

1. Add your bot to the admin group.
2. Send any message in that group.
3. Open this URL in a browser, replacing the token:

```text
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

4. Find the group `chat.id`. Group and supergroup IDs are usually negative, often starting with `-100`.

Example:

```env
ADMIN_CHAT_ID=-1001234567890
```

## Get `PUBLISH_CHAT_ID`

`PUBLISH_CHAT_ID` is the public channel or group where approved submissions are published.

For a group:

1. Add the bot to the group.
2. Send a message in the group.
3. Use `getUpdates` as above and copy `chat.id`.

For a channel:

1. Add the bot as an admin of the channel.
2. Give it permission to post messages.
3. Send a test post or forward a channel post where the bot can see updates.
4. Use `getUpdates` and copy the channel `chat.id`.

Example:

```env
PUBLISH_CHAT_ID=-1009876543210
```

## Get `ADMIN_USER_IDS`

`ADMIN_USER_IDS` controls who may approve, reject, or edit submissions. The bot checks the Telegram user ID of the person clicking the inline button, even inside the private admin chat.

Ways to find a user ID:

- Message `@userinfobot` or a similar Telegram ID helper bot.
- Send a private message to your bot, then inspect `from.id` in:

```text
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

Multiple admins are comma-separated:

```env
ADMIN_USER_IDS=111111111,222222222,333333333
```

## Install Dependencies

Use Python 3.11 or newer.

```bash
cd marvel-rivals-ua-submission-bot
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On macOS or Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Create `.env`

Copy `.env.example` to `.env` and fill in real values:

```env
BOT_TOKEN=1234567890:your_real_token_here
ADMIN_CHAT_ID=-1001234567890
PUBLISH_CHAT_ID=-1009876543210
ADMIN_USER_IDS=111111111,222222222
SUBMISSION_COOLDOWN_SECONDS=120
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
OFFICIAL_NEWS_URL=https://www.marvelrivals.com/news/
NEWS_CHECK_INTERVAL_MINUTES=30
ARTICLE_TIMEZONE=Europe/Kyiv
```

Optional:

```env
DATABASE_PATH=bot.db
```

If `DATABASE_PATH` is omitted, the bot creates `bot.db` in the project directory.

`SUBMISSION_COOLDOWN_SECONDS` controls how often one Telegram user may submit content. The default is `120`. Set it to `0` to disable the limit.

`GEMINI_API_KEY` enables AI draft generation for official news. Create a key in Google AI Studio at `https://aistudio.google.com/app/apikey`, then paste it into `.env`. If this value is missing, the bot still starts and manual submissions still work; the collector logs a clear warning and skips AI draft generation.

`GEMINI_MODEL` controls which Gemini model creates Ukrainian Telegram drafts. The recommended default is `gemini-2.5-flash`. The prompt asks Gemini to create concise, readable posts, convert UTC schedules to Kyiv time when reliable, and follow the glossary rules in `prompts/gemini_news_uk.md`.

`OFFICIAL_NEWS_URL` controls the official Marvel Rivals news list URL. The default is `https://www.marvelrivals.com/news/`.

`NEWS_CHECK_INTERVAL_MINUTES` enables the background collector scheduler when it is a positive integer and `GEMINI_API_KEY` is set. The default is `30`. Leave it empty or set an invalid value to disable automatic scheduled checks.

`ARTICLE_TIMEZONE` controls article date conversion for admin metadata and reliable in-article schedules. The default is `Europe/Kyiv`; public event/shop/patch/maintenance times are shown as Kyiv time with `за Києвом` wording when conversion is reliable.

The community navigation footer is always appended to every published post. Its labels and URLs both live in `locales/uk.json` under `post_footer.links` — there is no environment variable for it. See [Official News Collector](#official-news-collector) for details.

## Run Locally

```bash
python main.py
```

On startup the bot validates required environment variables, initializes SQLite tables, and starts polling Telegram.

## Test the Full Flow

1. Start the bot locally.
2. Open the bot in Telegram and send `/start`.
3. Send a text post or link.
4. Confirm the bot replies:

```text
Дякуємо! Твою новину відправлено на модерацію.
```

5. Check the admin chat for the original post content first, then a metadata message with buttons.
6. Click `✏️ Edit` from an allowed admin account.
7. Choose the part to edit, update the copied draft post, then click `💾 Зберегти`.
8. Confirm the original post message above is updated and the metadata message still has approve/edit/reject buttons.
9. Click `✅ Approve`.
10. Confirm the post appears in `PUBLISH_CHAT_ID`.
11. Send another submission and click `❌ Reject`.
12. Confirm it is not published and the admin preview status changes to `rejected`.
13. Try clicking a button from a Telegram user ID not listed in `ADMIN_USER_IDS`. The bot should show an alert and do nothing.
14. Send two submissions from the same user within `SUBMISSION_COOLDOWN_SECONDS`. The second one should be rejected with a wait message.

## Official News Collector

The official news collector reads `https://www.marvelrivals.com/news/`, extracts recent article cards, fetches individual article pages, parses the title, canonical URL, date, article text, and a safe cover image/photo when available, then asks Gemini to create a Ukrainian Telegram-ready draft. Drafts are intentionally concise, usually one moderation post around 400-900 characters, so a long article does not become a noisy 6-10 part moderation batch.

The generated public draft includes only publishable post content and official hashtags. Publication date, source type, article title, status, and raw `source_url` are admin-only metadata. Public posts do not show raw source URLs; source attribution is rendered as `Повні деталі — на офіційному сайті.`, where `офіційному сайті` is a Telegram HTML link to the stored `source_url`.

Nothing is auto-published. Each generated item is inserted into `submissions` with `status = "pending"` and sent to the existing admin moderation queue with the same `✅ Approve`, `✏️ Edit`, and `❌ Reject` buttons. When a draft must be split, the parts stay under one metadata/control message, but official news is biased toward one concise preview. Admins can edit each part before publishing.

Run a manual check from an allowed admin account:

```text
/fetch_news
```

The command renders source buttons. For now there is one button: official Marvel Rivals site. After an admin clicks a source, the bot sends a parsing-started status message, then processes one latest unparsed article from that source and reports how many articles were found, how many were already seen, how many were new, how many drafts were created, how many were sent to moderation, and how many failed. Only users listed in `ADMIN_USER_IDS` can run it.

Collectors are registered in `services/collectors/registry.py`. The scheduler runs every registered collector when `NEWS_CHECK_INTERVAL_MINUTES` is enabled, so future Reddit or gaming-media collectors can be added to the registry and will be included in scheduled checks automatically. Scheduled runs process unseen articles from the latest stored article publication date in `seen_sources.article_date`, not from the time the bot last parsed the source.

The Gemini wrapper prompt lives in `prompts/gemini_news_uk.md`. The editable official-news style guide lives in `prompts/official_news_style.md`; update that file to tune tone, templates, length limits, source URL rules, tag rules, and Kyiv-time wording without changing Python code. User-visible UI text, reports, date labels, footer labels, and collector button labels live in `locales/uk.json`.

Official AI posts are styled as Telegram gaming-community updates, not article summaries. The prompt asks for a short headline, 1-3 compact blocks, relevant emoji markers, natural Ukrainian, no greetings, no clickbait, no raw Markdown, no public metadata, and no copied patch-note wall. Post-processing sanitizes Markdown artifacts such as `**bold**`, `*` bullets, raw headings, excessive asterisks, duplicated blank lines, misplaced hashtags, raw source URLs, and public `Дата публікації` / `Джерело` lines before moderation. The code detects broad article types from title/body keywords and passes the matching style context to Gemini: shop/skins/bundles, event/rewards/login bonus, patch notes/game update, trailer/teaser/map reveal, vote/community choice, or short announcement. Normal posts target 400-900 characters and are capped at 1200; large patch notes are capped at 1600 and should use 3-5 grouped highlights.

The admin moderation preview keeps metadata separate from the publishable draft. For official news it shows the submission ID, source, article title, detected category, Kyiv article date when available, source URL, truncated draft preview, tags, status, and the normal approve/edit/reject buttons. It does not dump the full parsed article body into Telegram; full `original_text` remains stored in the database for context.

The community navigation footer is always added to every published post — official AI-generated news drafts (after the hashtags) and manual user submissions alike. It is shown in moderation so admins see the final publishable post:

```text
#MarvelRivalsUA #Офіційно #Анонс

---
Навігація по ком’юніті 👇
💬 Чат | 🤖 Запропонувати новину | 🎧 Discord
```

Footer labels and URLs both live in `locales/uk.json` under `post_footer.links` (`chat`, `submission`, `discord`), each with a `label` and a `url`. If a `url` is set, that item is rendered as a safe Telegram HTML link; if a `url` is empty or invalid, the item stays plain text. The bot validates that footer URLs use `http` or `https` before rendering links. Admins can still edit the visible footer text while editing the draft before approval.

Published, moderated, and edited text messages are sent with Telegram link previews disabled. This keeps hidden footer links and any body links from creating large embedded preview cards. Telegram sends use a 30 second request timeout and retry retryable network failures, flood-wait responses, `TimeoutError`, and `aiohttp.ClientOSError` up to three times with exponential backoff. A short delay is added between multi-message sends to reduce flood risk.

Duplicate detection uses the `seen_sources` table. For official Marvel Rivals news, `source_type` is `official_marvel_rivals` and `source_id` is the canonical article URL. An article is marked as seen only after Gemini creates a draft and the moderation preview is sent successfully. If Gemini fails, Telegram moderation sending fails, or the bot crashes before moderation succeeds, the article is not marked as seen and `/fetch_news` can try it again later.

AI-generated official news tags are deterministic. Every official article gets `#MarvelRivalsUA` and `#Офіційно`, then up to three Ukrainian topic hashtags based on the title/body, such as `#Патч`, `#Фікси`, `#Баланс`, `#Івент`, `#Магазин`, `#Скіни`, `#Герої`, `#Карта`, `#Геймплей`, `#Сезон`, `#ТехнічніРоботи`, `#Рейтинг`, `#Трейлер`, `#Голосування`, `#Анонс`, or `#Кіберспорт`. If nothing specific matches, the fallback is `#MarvelRivalsUA #Офіційно #Анонс`.

Media parsing supports `media_type = "photo"` and `media_type = "none"`. The collector prefers Open Graph images, Twitter card images, list/article cover images, and meaningful article images. It filters obvious logos, icons, tracking pixels, avatars, and generic site images. The first safe image remains the primary media. If multiple meaningful images are parsed and the draft has multiple real parts, later parts can receive later media URLs; a normal article still usually creates one media moderation message. If media cannot be parsed safely, the draft is still created as text-only. If sending external media to Telegram moderation fails after retries, the bot logs the error and falls back to a text-only moderation message.

When an approved AI news item has `media_url` and `media_type = "photo"`, the publisher sends the photo URL directly to Telegram with the edited draft as a caption when it fits. If the caption is too long for Telegram, it publishes the image first and then the edited text as one separate message. If the item has multiple saved parts with relevant media on later parts, those parts are published in order using the same caption rules. If Telegram rejects an external media URL itself, the bot logs the error and publishes the text-only fallback. The bot does not download collector media to disk; it stores only Telegram `file_id` values for user-submitted media and external `media_url` values for collected news.

Article dates are parsed with `python-dateutil`. If the source includes timezone information, the date is converted to `ARTICLE_TIMEZONE` with `zoneinfo`. If a source date includes a time but no timezone, the bot does not assume UTC; it keeps date-only metadata instead. Public posts should show visible times only as Kyiv time with `за Києвом` when conversion is reliable. Common `HH:MM UTC/GMT` event schedules from article text are converted to Kyiv time and supplied to Gemini as notes; post-processing also replaces matching raw UTC/GMT times when those notes are available. If conversion is uncertain, the prompt tells Gemini to avoid guessing and omit the time or keep date-only wording.

The current collector supports only the official Marvel Rivals news page. Reddit collection is planned as a future source.

## Editing Text And Media

The moderation chat now keeps the publishable post content separate from the control panel:

- The first message or messages are the current post parts without buttons.
- Every post part message renders the community footer with clickable Telegram HTML links, matching the publish preview, for both official AI news and manual submissions.
- The last message is the metadata/control message with `✅ Approve`, `✏️ Edit`, and `❌ Reject`.
- After `✏️ Edit`, the bot shows buttons for every part, even when there is only one part.
- Choosing a part copies that original message into a temporary draft message with `💾 Зберегти`.
- After the admin edits the draft message and clicks save, the draft is deleted and the original part message above is updated.
- In groups where admins cannot directly edit bot messages, sending a new text message while the draft is active updates the draft message; the admin still confirms with `💾 Зберегти`.
- `➕ Нова частина` starts add-part mode. The next supported admin message is copied into the moderation chat as a new post part, and the metadata/control message is moved to the bottom again.
- Approval publishes all saved parts in order.

## Discord Moderation (Optional)

The project can also run an optional Discord moderation bot for our own Marvel
Rivals UA community server. It is **independent** from the Telegram flow and is
**not** a news source — Discord is used only for moderation/utility because the
official server does not expose Follow Channel access.

Both bots run in the **same process**: `main.py` starts the Discord bot as a
background asyncio task alongside the existing Telegram long-polling loop. There
is still only one `asyncio.run()`. If the Discord bot is disabled, misconfigured,
or fails to log in, it logs a safe message and the Telegram bot keeps running
normally. Secrets are never logged.

### Enable it

The Discord module lives in [`discord_moderation.py`](discord_moderation.py) and
uses `discord.py`. It starts only when `ENABLE_DISCORD_MODERATION=true`. Add to
`.env`:

```env
ENABLE_DISCORD_MODERATION=true
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_MOD_LOG_CHANNEL_ID=123456789012345678
DISCORD_ALLOWED_INVITES=
DISCORD_GUILD_ID=
DISCORD_WELCOME_CHANNEL_ID=
DISCORD_RULES_CHANNEL_ID=
DISCORD_CHAT_CHANNEL_ID=
DISCORD_LFT_CHANNEL_ID=
```

- `ENABLE_DISCORD_MODERATION` — anything other than `true` keeps the Discord bot off.
- `DISCORD_BOT_TOKEN` — bot token from the Discord Developer Portal. Keep it secret; it is read only from the environment and never printed or logged.
- `DISCORD_MOD_LOG_CHANNEL_ID` — channel where moderation actions, reports, and warning auto-actions are logged (enable Developer Mode, right-click the channel → Copy Channel ID). If it is missing or the bot cannot post there, the bot logs a single safe warning instead.
- `DISCORD_ALLOWED_INVITES` — optional, comma-separated invite codes or full invite URLs to allow. **Empty means block every Discord invite link.**
- `DISCORD_GUILD_ID` — optional. When set, slash commands sync instantly to that server; otherwise a global sync is used, which can take up to ~1 hour to appear.
- `DISCORD_WELCOME_CHANNEL_ID` — optional. When set, the bot greets each new member in that channel. **This requires the privileged "Server Members Intent" enabled in the Developer Portal.** Leave it empty to disable public welcomes; the bot then does not request the members intent at all, so the rest of the bot is unaffected.
- `DISCORD_RULES_CHANNEL_ID`, `DISCORD_CHAT_CHANNEL_ID`, `DISCORD_LFT_CHANNEL_ID` — optional channel IDs shown as links in the welcome message. Each appears only when set; channels are never hardcoded.

Misconfigured Discord values never raise a configuration error, so they cannot
stop the Telegram bot from starting.

### Developer Portal setup (one-time)

1. **Message Content Intent** — enable it under *Developer Portal → your app → Bot → Privileged Gateway Intents*. Without it the bot logs in but cannot read messages, so the filters cannot work. The bot detects this case and logs a clear hint.
2. **Slash command scopes** — invite the bot with **both** the `bot` and `applications.commands` OAuth2 scopes, e.g. an invite URL containing `scope=bot%20applications.commands`. Without `applications.commands` the slash commands will not appear.
3. **Server Members Intent** — only required if you enable the welcome system (`DISCORD_WELCOME_CHANNEL_ID`). Enable it in the same *Privileged Gateway Intents* section. The bot requests this intent only when a welcome channel is configured, so leaving welcome off needs no extra setup.

### Required Discord permissions (no Administrator)

Grant only these — the bot is designed to work without Administrator and degrades
gracefully when a permission is missing:

- View Channels
- Send Messages
- Manage Messages (delete flagged messages, `/clear`)
- Read Message History
- Moderate Members (timeouts, `/timeout`)
- Embed Links (mod-log embeds)
- Use Application Commands (slash commands)

Double-check that the bot's role is **above** the roles of members it should be
able to time out — Discord forbids timing out the server owner or anyone with an
equal/higher role, regardless of permissions.

### Moderation features

- **Anti-spam** — tracks messages per user per channel; more than 5 messages in ~7 seconds deletes the triggering message (when the bot has Manage Messages), applies a 60-second timeout (when it has Moderate Members), and logs the action. Thresholds are constants near the top of `discord_moderation.py`.
- **Invite filter** — detects `discord.gg/…`, `discord.com/invite/…`, and `discordapp.com/invite/…` and deletes them unless the code is in `DISCORD_ALLOWED_INVITES`.
- **Suspicious link filter** — conservative patterns for fake-Nitro, Steam-gift scams, crypto/airdrop spam, phishing look-alike domains, and IP-logger/grabber links; generic URL shorteners are flagged only when paired with a scam keyword.
- **Bad-word filter** — a deliberately tiny, configurable list kept in an external, easy-to-edit text file, [`discord_badwords.txt`](discord_badwords.txt) (one term per line, `#` comments allowed). It is matched with Unicode-aware word boundaries to avoid overblocking normal Ukrainian/Russian/English words. Add server-specific terms there and restart the bot; if the file is missing the bot falls back to a small built-in default.
- **Mod logs** — every action is posted to `DISCORD_MOD_LOG_CHANNEL_ID` as an embed with the action, user mention + ID, channel, reason, a short safe message preview, and a timestamp. The log never pings anyone.

Members who already have **Manage Messages** (mods/admins) bypass the content
filters.

### Slash commands

All command descriptions and user-facing replies are in Ukrainian.

- `/help` — show the full command list (visible to moderators only).
- `/clear amount` — delete 1–100 recent messages in the current channel (requires Manage Messages).
- `/timeout user minutes reason` — timeout a member for 1–40320 minutes / up to 28 days (requires Moderate Members).
- `/warn user reason` — warn a member; stored in persistent history, logged to the mod-log channel, and DM'd to the user when possible (requires Moderate Members).
- `/warnings user` — show a member's warning history (requires Moderate Members).
- `/clearwarnings user` — clear a member's warning history (requires Moderate Members).
- `/report member reason` — **available to any member**; sends a report to the mod-log channel. The reporter gets an ephemeral confirmation and a light per-user cooldown prevents spam.

Each moderator command re-checks permissions at runtime and reports problems
privately (ephemeral) instead of failing loudly.

### Warning history and auto-actions

`/warn` records each warning in the project's SQLite database in a dedicated
`discord_warnings` table (columns: `id`, `guild_id`, `user_id`, `moderator_id`,
`reason`, `created_at`). The table is created automatically on startup; no
migration step is needed and the Telegram side never touches it.

Auto-actions trigger on the number of active warnings (thresholds are constants
near the top of `discord_moderation.py`):

- 3 warnings → 10-minute timeout
- 5 warnings → 1-hour timeout
- 7 warnings → an urgent mod-log note asking moderators to review manually

The bot **never auto-bans or auto-kicks**, only times out when it has *Moderate
Members* (logging a safe note to the mod-log if it cannot), and never
auto-punishes members who themselves have moderator/admin permissions.

### Welcome system (optional)

When `DISCORD_WELCOME_CHANNEL_ID` is set, the bot greets each new member in that
channel with a Ukrainian welcome embed, optionally linking the rules / chat /
looking-for-team channels when their IDs are configured. It mentions only the
joining member and never `@everyone`. This is the only feature that needs the
privileged Server Members Intent, and it is requested only when the welcome
channel is configured.

### Install and run

`discord.py` is listed in `requirements.txt`, so `pip install -r requirements.txt`
installs it. Run the project exactly as before with `python main.py` — when
`ENABLE_DISCORD_MODERATION=true` and a valid token is present, the Discord bot
starts automatically next to the Telegram bot. To run Telegram only, set
`ENABLE_DISCORD_MODERATION=false` (or leave the token empty).

## Current Limitations

- Polling mode only, no webhook setup.
- No role management inside Telegram. Admin access comes only from `ADMIN_USER_IDS`.
- Only the official Marvel Rivals news page is supported as an automated source.
- Official news media support is photo-only; videos are not parsed yet.
- User submissions are rate-limited per Telegram user ID using the latest saved submission timestamp.
- Admin content parts stay in the moderation chat above the metadata/control message.
- If the moderation chat is a Telegram channel, Telegram does not expose the author of ordinary channel posts to the bot. The bot enforces `ADMIN_USER_IDS` on inline button clicks, then accepts the next supported channel post only for the explicit `➕ Нова частина` flow. Use a private group or supergroup if you need every add-part message to carry the real admin user ID.
- There is no special command for clearing a text-only part to empty.

## Planned Future Features

- Reddit parser.
- Gaming media source parsers.
- Richer media support for videos when it can be detected safely.
- Webhook deployment mode.
