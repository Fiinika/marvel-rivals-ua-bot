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
- Sends an admin moderation preview with submission metadata and inline buttons.
- Allows only configured admin user IDs to approve, reject, or edit.
- Publishes approved text or original media to the configured public chat.
- Handles manual long media captions by publishing the media first, then the draft text as a separate message. Official-news media posts are split before moderation so approved media and caption can publish as one Telegram message.
- Supports per-admin edit state and `/cancel` for edit cancellation.
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

In channel-based moderation, the admin user ID is checked when `✏️ Edit` is clicked. After that, the next text, photo, video, or document post in the admin channel is treated as the edit for the active submission. The moderation preview is updated, edit mode is closed, and the temporary edit messages are deleted when possible.

Telegram can show modal alerts only after inline button clicks, not after ordinary text messages. After edited text is received, the confirmation is the updated moderation preview itself.

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

5. Check the admin chat for a moderation preview with buttons.
6. Click `✏️ Edit` from an allowed admin account.
7. Send the edited content:
   - in the admin chat, if `ADMIN_CHAT_ID` is a group or supergroup
   - as the next post in the admin channel, if `ADMIN_CHAT_ID` is a channel
8. Confirm the preview updates and still has approve/edit/reject buttons.
9. Click `✅ Approve`.
10. Confirm the post appears in `PUBLISH_CHAT_ID`.
11. Send another submission and click `❌ Reject`.
12. Confirm it is not published and the admin preview status changes to `rejected`.
13. Try clicking a button from a Telegram user ID not listed in `ADMIN_USER_IDS`. The bot should show an alert and do nothing.
14. Send two submissions from the same user within `SUBMISSION_COOLDOWN_SECONDS`. The second one should be rejected with a wait message.

## Official News Collector

The official news collector reads `https://www.marvelrivals.com/news/`, extracts recent article cards, fetches individual article pages, parses the title, canonical URL, date, article text, and a safe cover image/photo when available, then asks Gemini to create a Ukrainian Telegram-ready draft. The draft is intentionally not compressed to a few sentences; long official articles are grouped into readable sections and bullet lists.

The generated public draft does not include the publication date or source URL. Those fields stay available to admins in the moderation preview as `source_url`, `article_date_display`, and in the full `original_text` context.

Nothing is auto-published. Each generated item is inserted into `submissions` with `status = "pending"` and sent to the existing admin moderation queue with the same `✅ Approve`, `✏️ Edit`, and `❌ Reject` buttons. Admins can edit the generated draft before publishing.

Run a manual check from an allowed admin account:

```text
/fetch_news
```

The command renders source buttons. For now there is one button: official Marvel Rivals site. After an admin clicks a source, the bot sends a parsing-started status message, then parses one latest unparsed article from that source and reports how many articles were found, how many were already seen, how many were new, how many drafts were created, how many were sent to moderation, and how many failed. Only users listed in `ADMIN_USER_IDS` can run it.

Collectors are registered in `services/collectors/registry.py`. The scheduler runs every registered collector when `NEWS_CHECK_INTERVAL_MINUTES` is enabled, so future Reddit or gaming-media collectors can be added to the registry and will be included in scheduled checks automatically. Scheduled runs process unseen articles from the latest stored article publication date in `seen_sources.article_date`, not from the time the bot last parsed the source.

The Gemini prompt lives in `prompts/gemini_news_uk.md`. User-visible UI text, reports, date labels, footer labels, and collector button labels live in `locales/uk.json`.

Generated official-news posts get a community navigation footer after the post body and hashtags. Footer text, labels, and URLs live in `locales/uk.json` under `post_footer`, so you can update the visible labels and real community links without touching Python code. On publication the footer is rendered with Telegram HTML links, so admins see labels such as `💬 Чат`, while Telegram opens the configured URLs:

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

Duplicate detection uses the `seen_sources` table. For official Marvel Rivals news, `source_type` is `official_marvel_rivals` and `source_id` is the canonical article URL. An article is marked as seen only after Gemini creates a draft and the moderation preview is sent successfully. If Gemini fails, Telegram moderation sending fails, or the bot crashes before moderation succeeds, the article is not marked as seen.

Media parsing supports `media_type = "photo"` and `media_type = "none"`. The collector prefers Open Graph images, Twitter card images, list/article cover images, and then the first meaningful article image. It filters obvious logos, icons, tracking pixels, avatars, and generic site images. If media cannot be parsed safely, the draft is still created as text-only. If sending a media preview to Telegram moderation fails, the bot logs the error and falls back to a text-only moderation preview.

When an approved AI news item has `media_url` and `media_type = "photo"`, the publisher sends the photo URL directly to Telegram with the edited draft as a caption. AI drafts are split before moderation so the media part fits Telegram's caption limit and can publish as one photo-caption message. If an admin edits the caption beyond Telegram's caption limit, publication fails with an admin alert instead of splitting media and text into separate public posts. If Telegram rejects the external media URL itself, the bot logs the error and publishes the text-only fallback. The bot does not download collector media to disk; it stores only Telegram `file_id` values for user-submitted media and external `media_url` values for collected news.

Article dates are parsed with `python-dateutil`. If the source includes timezone information, the date is converted to `ARTICLE_TIMEZONE`. If the source date has no timezone, the collector assumes UTC. Date display uses Ukrainian month names; for the default timezone it looks like `Дата публікації: 29 травня 2026, 18:30 за Києвом`. If only a date is available, it omits the time. If parsing fails, the date is omitted rather than guessed.

The current collector supports only the official Marvel Rivals news page and only a single cover image/photo for official news posts. Reddit collection is planned as a future source.

## Editing Text And Media

After an allowed admin clicks `✏️ Edit`, the next supported message becomes the edit:

- Send plain text to replace `Поточна чернетка`.
- Send a photo to replace the submission media with that photo.
- Send a video to replace the submission media with that video.
- Send a document to replace the submission media with that document.

For media edits:

- If the media has a caption, that caption becomes the new `Поточна чернетка`.
- If the media has no caption, the current draft text stays unchanged.
- The new media message stays in the moderation chat.
- The bot marks the new media as added to the draft.
- The bot marks the previous media as replaced and no longer publishable.
- The admin preview updates `Тип`, `Файл`, `Медіа-повідомлення`, `Поточна чернетка`, and `Оновлено`.
- Approval publishes the updated media and current draft text.

## Current Limitations

- Polling mode only, no webhook setup.
- No role management inside Telegram. Admin access comes only from `ADMIN_USER_IDS`.
- Only the official Marvel Rivals news page is supported as an automated source.
- Only cover image/photo media is supported for official news posts.
- User submissions are rate-limited per Telegram user ID using the latest saved submission timestamp.
- Admin media stays in the moderation chat and is marked by the bot when it is original, newly added, or replaced. The editable moderation control panel remains a separate text message.
- If the moderation chat is a Telegram channel, Telegram does not expose the author of the next channel post to the bot. The bot enforces `ADMIN_USER_IDS` on the `✏️ Edit` click, then accepts the next supported post in that channel as the edit. Use a private group or supergroup if you need every edit message to carry the real admin user ID.
- There is no special command for clearing a media caption to empty. Sending media without a caption keeps the current draft text.

## Planned Future Features

- Reddit parser.
- Gaming media source parsers.
- Richer media support for videos when it can be detected safely.
- Webhook deployment mode.
