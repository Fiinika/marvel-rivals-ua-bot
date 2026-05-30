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
- Handles manual long media captions by publishing the media first, then the draft text as a separate message. Official-news media posts are split before moderation so approved media and caption can publish as one Telegram message.
- Supports part-based editing, adding new post parts, and `/cancel` for active edit prompts.
- Limits user submissions with configurable per-user cooldown.
- Fetches the official Marvel Rivals news page and creates Ukrainian Gemini drafts for new articles.
- Sends every AI-generated official news draft to the same admin moderation queue as manual submissions.
- Tracks already moderated official articles in SQLite to avoid duplicate drafts.
- Supports official news cover images/photos when a safe media URL is detected.

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

`GEMINI_MODEL` controls which Gemini model creates Ukrainian Telegram drafts. The recommended default is `gemini-2.5-flash`, which is better suited here than Flash-Lite because these drafts need longer structured output and stronger instruction-following. The prompt asks Gemini to create fuller, clearer posts, split large articles into logical parts, and follow the glossary rules in `prompts/gemini_news_uk.md`.

`OFFICIAL_NEWS_URL` controls the official Marvel Rivals news list URL. The default is `https://www.marvelrivals.com/news/`.

`NEWS_CHECK_INTERVAL_MINUTES` enables the background collector scheduler when it is a positive integer and `GEMINI_API_KEY` is set. The default is `30`. Leave it empty or set an invalid value to disable automatic scheduled checks.

`ARTICLE_TIMEZONE` controls article date conversion for drafts and metadata. The default is `Europe/Kyiv`.

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

The official news collector reads `https://www.marvelrivals.com/news/`, extracts recent article cards, fetches individual article pages, parses the title, canonical URL, date, article text, and a safe cover image/photo when available, then asks Gemini to create a Ukrainian Telegram-ready draft. The draft is intentionally not compressed to a few sentences; long official articles are grouped into readable sections and bullet lists.

The generated public draft does not include the publication date or source URL. Those fields stay available to admins in the moderation preview as `source_url`, `article_date_display`, and in the full `original_text` context.

Nothing is auto-published. Each generated item is inserted into `submissions` with `status = "pending"` and sent to the existing admin moderation queue with the same `✅ Approve`, `✏️ Edit`, and `❌ Reject` buttons. When Gemini creates multiple logical parts, they stay under one metadata/control message. Admins can edit each part before publishing.

Run a manual check from an allowed admin account:

```text
/fetch_news
```

The command renders source buttons. For now there is one button: official Marvel Rivals site. After an admin clicks a source, the bot sends a parsing-started status message, then processes one latest unparsed article from that source and reports how many articles were found, how many were already seen, how many were new, how many drafts were created, how many were sent to moderation, and how many failed. Only users listed in `ADMIN_USER_IDS` can run it.

Collectors are registered in `services/collectors/registry.py`. The scheduler runs every registered collector when `NEWS_CHECK_INTERVAL_MINUTES` is enabled, so future Reddit or gaming-media collectors can be added to the registry and will be included in scheduled checks automatically. Scheduled runs process unseen articles from the latest stored article publication date in `seen_sources.article_date`, not from the time the bot last parsed the source.

The Gemini prompt lives in `prompts/gemini_news_uk.md`. User-visible UI text, reports, date labels, footer labels, and collector button labels live in `locales/uk.json`.

Every post part gets a community navigation footer after the post body, whether it came from the official-news parser, manual submission, manual edit, or final publication. Footer text, labels, and URLs live in `locales/uk.json` under `post_footer`, so you can update the visible labels and real community links without touching Python code. The footer is rendered with Telegram HTML links, so admins and public subscribers see labels such as `💬 Чат`, while Telegram opens the configured URLs:

```json
"post_footer": {
  "separator": "━━━━━━━━━━━━━━━━━━━━",
  "title": "Навігація по комʼюніті 👇",
  "links": {
    "chat": { "label": "💬 Чат", "url": "https://t.me/UAMarvelRivalsChat" },
    "submission": { "label": "🤖 Запропонувати новину", "url": "https://t.me/MarvelRivalsUABot" },
    "discord": { "label": "🎧 Discord", "url": "https://discord.gg/953cRRVD" }
  }
}
```

When a URL is empty, the footer shows only the label.

Published, moderated, and edited text messages are sent with Telegram link previews disabled. This keeps hidden footer links and any body links from creating large embedded preview cards.

Duplicate detection uses the `seen_sources` table. For official Marvel Rivals news, `source_type` is `official_marvel_rivals` and `source_id` is the canonical article URL. An article is marked as seen only after Gemini creates a draft and the moderation preview is sent successfully. If Gemini fails, Telegram moderation sending fails, or the bot crashes before moderation succeeds, the article is not marked as seen.

AI-generated news tags are stored in SQLite. New tag names are inserted into `tags`, and each submission is linked through `submission_tags`. The admin preview reads tags back from the database and shows them near the article metadata. Gemini returns these tags in a service-only `---TAGS---` block, so they do not appear in the public Telegram post unless an admin manually adds them.

Media parsing supports `media_type = "photo"` and `media_type = "none"`. The collector prefers Open Graph images, Twitter card images, list/article cover images, and then the first meaningful article image. It filters obvious logos, icons, tracking pixels, avatars, and generic site images. If media cannot be parsed safely, the draft is still created as text-only. If sending a media part to Telegram moderation fails, the bot logs the error and falls back to a text-only moderation message.

When an approved AI news item has `media_url` and `media_type = "photo"`, the publisher sends the photo URL directly to Telegram with the edited draft as a caption. AI drafts are split before moderation so the media part fits Telegram's caption limit and can publish as one photo-caption message. If an admin edits the caption beyond Telegram's caption limit, publication fails with an admin alert instead of splitting media and text into separate public posts. If Telegram rejects the external media URL itself, the bot logs the error and publishes the text-only fallback. The bot does not download collector media to disk; it stores only Telegram `file_id` values for user-submitted media and external `media_url` values for collected news.

Article dates are parsed with `python-dateutil`. If the source includes timezone information, the date is converted to `ARTICLE_TIMEZONE`. If the source date has no timezone, the collector assumes UTC. Date display uses Ukrainian month names and the real Kyiv UTC offset from `zoneinfo`: `UTC+2` in winter and `UTC+3` in summer. For the default timezone it looks like `Дата публікації: 29 травня 2026, 18:30 за Києвом (UTC+3)`. If only a date is available, it omits the time. If parsing fails, the date is omitted rather than guessed.

The current collector supports only the official Marvel Rivals news page and only a single cover image/photo for official news posts. Reddit collection is planned as a future source.

## Editing Text And Media

The moderation chat now keeps the publishable post content separate from the control panel:

- The first message or messages are the current post parts without buttons.
- These post part messages render the community footer with clickable Telegram HTML links, matching the publish preview.
- The last message is the metadata/control message with `✅ Approve`, `✏️ Edit`, and `❌ Reject`.
- After `✏️ Edit`, the bot shows buttons for every part, even when there is only one part.
- Choosing a part copies that original message into a temporary draft message with `💾 Зберегти`.
- After the admin edits the draft message and clicks save, the draft is deleted and the original part message above is updated.
- In groups where admins cannot directly edit bot messages, sending a new text message while the draft is active updates the draft message; the admin still confirms with `💾 Зберегти`.
- `➕ Нова частина` starts add-part mode. The next supported admin message is copied into the moderation chat as a new post part, and the metadata/control message is moved to the bottom again.
- Approval publishes all saved parts in order.

## Current Limitations

- Polling mode only, no webhook setup.
- No role management inside Telegram. Admin access comes only from `ADMIN_USER_IDS`.
- Only the official Marvel Rivals news page is supported as an automated source.
- Only cover image/photo media is supported for official news posts.
- User submissions are rate-limited per Telegram user ID using the latest saved submission timestamp.
- Admin content parts stay in the moderation chat above the metadata/control message.
- If the moderation chat is a Telegram channel, Telegram does not expose the author of ordinary channel posts to the bot. The bot enforces `ADMIN_USER_IDS` on inline button clicks, then accepts the next supported channel post only for the explicit `➕ Нова частина` flow. Use a private group or supergroup if you need every add-part message to carry the real admin user ID.
- There is no special command for clearing a text-only part to empty.

## Planned Future Features

- Reddit parser.
- Gaming media source parsers.
- Richer media support for videos when it can be detected safely.
- Webhook deployment mode.
