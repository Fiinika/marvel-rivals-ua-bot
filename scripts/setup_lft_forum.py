"""One-off admin script: configure the LFT (looking-for-team) forum channel.

Run manually whenever the forum needs to be (re)configured:

    .venv\\Scripts\\python.exe scripts\\setup_lft_forum.py

What it does (plain Discord REST calls, no gateway connection):
  1. Finds the forum channel in DISCORD_GUILD_ID whose name contains
     "пошук" or "lft" (there is exactly one on our server).
  2. Renames it to "lft-пошук-тіммейтів" and sets the post guidelines,
     tags (platform / mode / role), default 🤝 reaction, list layout,
     sorting by latest activity and a 7-day auto-archive default.
  3. Creates the pinned "📌 Як знайти тіммейтів" intro post with the
     post template — unless a pinned 📌 post already exists (idempotent).
  4. Prints the forum channel ID so it can be put into DISCORD_LFT_CHANNEL_ID.

Required bot permissions: Manage Channels (step 2), Send Messages /
Create Posts (step 3), Manage Threads (pinning). The script only touches
this one forum channel and never prints the token.
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

API = "https://discord.com/api/v10"

FORUM_NAME = "lft-пошук-тіммейтів"

# Forum "Post Guidelines" (shown by Discord above the post list, max 4096).
FORUM_GUIDELINES = """🤝 Шукаєш, з ким пограти в Marvel Rivals? Ти за адресою!

Як створити пост:
• Натисни «New Post» і коротко напиши в назві, кого шукаєш — напр.: «Ranked Gold+, потрібен Strategist».
• Обери теги: платформа, режим, роль.
• Заповни шаблон із закріпленого поста 📌

Правила форуму:
1. Один активний пост на людину. Знайшов команду — напиши про це в пості й закрий його.
2. Без токсичності, образ і булінгу — граємо в задоволення 💙💛
3. Без реклами, бустів і продажу акаунтів.
4. Бачиш порушення — кидай /report, модерація розбереться.

GLHF! 🎮"""

# Forum tags: max 20 per forum, names up to 20 characters.
FORUM_TAGS = [
    {"name": "PC", "moderated": False, "emoji_id": None, "emoji_name": "💻"},
    {"name": "PlayStation", "moderated": False, "emoji_id": None, "emoji_name": "🎮"},
    {"name": "Xbox", "moderated": False, "emoji_id": None, "emoji_name": "🟢"},
    {"name": "Ranked", "moderated": False, "emoji_id": None, "emoji_name": "🏆"},
    {"name": "Casual", "moderated": False, "emoji_id": None, "emoji_name": "😎"},
    {"name": "Quick Match", "moderated": False, "emoji_id": None, "emoji_name": "⚡"},
    {"name": "Vanguard (танк)", "moderated": False, "emoji_id": None, "emoji_name": "🛡️"},
    {"name": "Duelist (дпс)", "moderated": False, "emoji_id": None, "emoji_name": "⚔️"},
    {"name": "Strategist (сапорт)", "moderated": False, "emoji_id": None, "emoji_name": "💉"},
]

INTRO_TITLE = "📌 Як знайти тіммейтів — почни звідси"

INTRO_CONTENT = """## Привіт! Це форум пошуку тіммейтів Marvel Rivals UA 🤝

**Як створити пост:**
1. Натисни **New Post / Новий пост**.
2. У назві коротко вкажи головне — режим, ранг, роль. Напр.: `Ranked Diamond+, шукаю Strategist`.
3. Обери теги: платформу, режим і роль.
4. Скопіюй шаблон нижче у свій пост і заповни 👇

**Шаблон:**
```
**Платформа:** PC / PlayStation / Xbox
**Режим:** Ranked / Casual / Quick Match
**Ранг:**
**Роль:** Vanguard (танк) / Duelist (дпс) / Strategist (сапорт)
**Час гри:** напр., 19:00–23:00 за Києвом
**Коментар:** вік, войс, кілька слів про себе
```

**Поради:**
• Один активний пост на людину. Знайшов команду — напиши про це в пості й закрий його 🔒
• Хочеш відгукнутися на чийсь пост? Просто напиши в ньому.
• Будь привітним — з токсиками грати ніхто не хоче 😉
• Бачиш порушення — скористайся командою `/report`.

GLHF! 🎮💙💛"""

GUILD_FORUM_TYPE = 15  # Discord channel type for forum channels
PINNED_THREAD_FLAG = 1 << 1  # forum-thread PINNED flag


FAILURES: list[str] = []


def fail(message: str) -> "None":
    print(f"ERROR: {message}")
    sys.exit(1)


def check(resp: httpx.Response, what: str) -> dict | list:
    if resp.status_code == 403:
        fail(f"{what}: 403 Forbidden — the bot is missing a permission. Body: {resp.text}")
    if resp.status_code >= 400:
        fail(f"{what}: HTTP {resp.status_code}. Body: {resp.text}")
    return resp.json() if resp.text else {}


def try_step(resp: httpx.Response, what: str, needs: str) -> dict | list | None:
    """Like check(), but a failure is recorded and the script keeps going."""
    if resp.status_code == 403:
        FAILURES.append(f"{what} — bot is missing the '{needs}' permission on the forum channel.")
        return None
    if resp.status_code >= 400:
        FAILURES.append(f"{what} — HTTP {resp.status_code}: {resp.text}")
        return None
    return resp.json() if resp.text else {}


def main() -> None:
    load_dotenv()
    token = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    guild_id = (os.getenv("DISCORD_GUILD_ID") or "").strip()
    if not token or not guild_id:
        fail("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")

    client = httpx.Client(
        headers={"Authorization": f"Bot {token}"},
        timeout=30.0,
    )

    # 1. Find the forum channel.
    channels = check(client.get(f"{API}/guilds/{guild_id}/channels"), "List guild channels")
    forums = [c for c in channels if c.get("type") == GUILD_FORUM_TYPE]
    candidates = [
        c for c in forums
        if "пошук" in c.get("name", "").lower() or "lft" in c.get("name", "").lower()
    ]
    if not candidates:
        fail(f"No forum channel matching 'пошук'/'lft' found. Forums present: {[c['name'] for c in forums]}")
    if len(candidates) > 1:
        fail(f"Several matching forums found, refusing to guess: {[c['name'] for c in candidates]}")
    forum = candidates[0]
    forum_id = forum["id"]
    print(f"Found forum: #{forum['name']} (id {forum_id})")

    # 2. Configure the channel itself.
    payload = {
        "name": FORUM_NAME,
        "topic": FORUM_GUIDELINES,
        "available_tags": FORUM_TAGS,
        "default_reaction_emoji": {"emoji_id": None, "emoji_name": "🤝"},
        "default_sort_order": 0,  # latest activity
        "default_forum_layout": 1,  # list view
        "default_auto_archive_duration": 10080,  # new posts auto-archive after 7 days
    }
    configured = try_step(
        client.patch(f"{API}/channels/{forum_id}", json=payload),
        "Configure forum channel (name/guidelines/tags/defaults)",
        needs="Manage Channels",
    )
    if configured is not None:
        print(f"Configured forum: name={FORUM_NAME}, {len(FORUM_TAGS)} tags, guidelines, 🤝 default reaction")

    # 3. Create + pin the intro post, unless a 📌 post already exists.
    active = check(client.get(f"{API}/guilds/{guild_id}/threads/active"), "List active threads")
    existing = [
        t for t in active.get("threads", [])
        if t.get("parent_id") == forum_id and t.get("name", "").startswith("📌")
    ]
    thread: dict | None
    if existing:
        thread = existing[0]
        print(f"Intro post already exists (id {thread['id']}); skipping creation.")
    else:
        thread = try_step(
            client.post(
                f"{API}/channels/{forum_id}/threads",
                json={
                    "name": INTRO_TITLE,
                    "auto_archive_duration": 10080,
                    "message": {"content": INTRO_CONTENT},
                },
            ),
            "Create intro post",
            needs="Send Messages / Create Posts",
        )
        if thread is not None:
            print(f"Created intro post (id {thread['id']})")

    if thread is not None:
        if thread.get("flags", 0) & PINNED_THREAD_FLAG:
            print("Intro post is already pinned.")
        else:
            pinned = try_step(
                client.patch(f"{API}/channels/{thread['id']}", json={"flags": PINNED_THREAD_FLAG}),
                "Pin intro post",
                needs="Manage Threads",
            )
            if pinned is not None:
                print("Pinned intro post.")

    print()
    if FAILURES:
        print("Some steps FAILED — grant the bot these permissions on the forum channel")
        print("(channel Edit -> Permissions -> add the bot/its role), then re-run this script:")
        for failure in FAILURES:
            print(f"  - {failure}")
        print()
    print("Add this to .env (and the production environment) so the bot")
    print("can link the forum in /lfthelp and welcome messages:")
    print(f"DISCORD_LFT_CHANNEL_ID={forum_id}")


if __name__ == "__main__":
    main()
