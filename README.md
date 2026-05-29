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
- Handles long media captions by publishing the media first, then the draft text as a separate message.
- Supports per-admin edit state and `/cancel` for edit cancellation.

## What It Does Not Do Yet

This first version is only for manual submissions and manual moderation. It does not implement:

- Reddit parsing
- official Marvel Rivals website parsing
- Gemini AI translation
- AI summaries
- auto-posting
- scheduled news fetching
- duplicate detection for external sources

The code is split into handlers and services so these features can be added later without mixing them into the moderation flow.

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
  formatter.py
  publisher.py
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
```

Optional:

```env
DATABASE_PATH=bot.db
```

If `DATABASE_PATH` is omitted, the bot creates `bot.db` in the project directory.

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

## Future Gemini Integration

Planned AI features should live outside the current manual moderation path, likely in a future `services/ai/` module. The preferred future Gemini model is:

```text
gemini-2.5-flash-lite
```

No Gemini API integration is implemented in this MVP.

## Current Limitations

- Polling mode only, no webhook setup.
- No role management inside Telegram. Admin access comes only from `ADMIN_USER_IDS`.
- No automated source ingestion.
- No duplicate detection.
- Admin media stays in the moderation chat and is marked by the bot when it is original, newly added, or replaced. The editable moderation control panel remains a separate text message.
- If the moderation chat is a Telegram channel, Telegram does not expose the author of the next channel post to the bot. The bot enforces `ADMIN_USER_IDS` on the `✏️ Edit` click, then accepts the next supported post in that channel as the edit. Use a private group or supergroup if you need every edit message to carry the real admin user ID.
- There is no special command for clearing a media caption to empty. Sending media without a caption keeps the current draft text.

## Planned Future Features

- Official Marvel Rivals news parser.
- Reddit parser.
- Gemini translation using `gemini-2.5-flash-lite`.
- AI summaries.
- Duplicate detection.
- Scheduled fetching.
- Optional auto-posting after trusted source checks.
- Webhook deployment mode.
