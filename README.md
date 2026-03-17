# TG-Sync-AutoForward-SaveRestricted-Bot

[中文文档 (Chinese README)](./README.zh-CN.md)

Telethon-based Telegram forwarding tool with a dual-client architecture (`Bot + UserBot`) for link parsing, historical sync, live monitoring, and resilient fallback strategies.

### 1. Features

- Private chat link parsing: public/private/comment/topic links
- `/sync` for historical backfill across channel, group, and group topic scopes
- `/monitor` for real-time forwarding across channel, group, and group topic scopes
- Automatic hard-block filtering during sync/forward:
  only filters messages/chats with cross-platform restriction (`platform=all`, e.g. `can't be displayed`);
  platform-specific restrictions (for example iOS/Android only) are still forwarded
- Unified fallback pipeline with automatic downgrade
- Supports both `copy` (default) and `forward`
- Supports `?comment=<id>` and `?single`
- Sends `#fail2forward` when all strategies fail

### 2. Architecture

- `Bot`: command entrypoint and target-side sending (write)
- `UserBot`: restricted-source reading and media download (read)
- Principle: `UserBot reads, Bot writes` whenever possible

### 3. Requirements

- Python 3.11+
- Telegram `api_id` / `api_hash`
- One Bot token
- One UserBot account (phone login)

### 4. Quick Start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python main.py
```

On first run, UserBot login verification is required. Session files are stored in `sessions/`.

### 5. Commands

- `/start`: startup guide
- `/help`: help
- `/sync <link> [--forward]`: sync historical messages to current target (supports channel / group / group topic)
- `/monitor <link> [--forward]`: monitor new messages and forward to current target (supports channel / group / group topic; requires UserBot to join source first)
- `/list`: manage tasks (pause/resume/delete/clear)
- `/settings`: view rate-limit settings

Sending a `t.me/...` link in private chat triggers parsing and forwarding.

### 6. Source-Type Permission Matrix

| Source Type | Bot Requirement / Role | UserBot Requirement (`/sync`) | UserBot Requirement (`/monitor`) |
|--|--|--|--|
| Public Channel | Bot sends to target; source may be readable directly by Bot | Usually no join required (readable is enough) | **Must join** |
| Private Channel | Bot usually cannot read source directly | Must be joined and readable | **Must be joined and readable** |
| Public Group | Bot sends to target; source may be readable directly by Bot | Usually no join required (readable is enough) | **Must join** |
| Private Group | Bot usually cannot read source directly | Must be joined and readable | **Must be joined and readable** |
| Group Topic (Forum Topic) | Bot sends into target topic/thread | Must be able to read the group, then fetch by `source_topic_id` | **Must join** |

Notes:

- `/monitor` validates whether UserBot has joined and can access the source before task creation.
- `/sync` usually does not require UserBot to pre-join public sources, but private/restricted sources still require UserBot access.

### 7. Forwarding Strategy (Priority Rules)

Runtime fallback order is always `1 -> 2 -> 3 -> 4`:

1. Strategy 1: Bot direct handling (highest priority)
2. Strategy 2: UserBot reads, then forward/copy
3. Strategy 3: UserBot downloads, Bot re-uploads
4. Strategy 4: fail marker `#fail2forward`

Operational priority interpretation:

- Priority A (best chance to stay on Strategy 1):
  Bot can send in target group/channel and is admin (private or public target).
- Priority B (often still Strategy 1):
  Source is a public channel/group, so Bot can usually read directly.
- Priority C (often downgraded to Strategy 3):
  Source is a private discussion group (comment area), or source has protected/no-forward content enabled.

Notes:

- `Strategy 1 success` in logs means the first layer succeeded, not necessarily native `forward`.
- In `copy` mode, Strategy 1 may internally use `send_message/send_file`.
- `?comment=` links point to messages in linked discussion groups, not in the channel post itself.

### 8. Additional Notes

- `forward` keeps native forwarding semantics; `copy` is often more compatible but may hit send-side limits faster.
- For private sources, UserBot must be a member and able to read.
- Topic delivery depends on target-side topic permissions (`target_topic_id`).
- Hard-block filtering uses a union rule for cross-platform blocking:
  if chat-level or message-level `restriction_reason.platform=all` is present, it is treated as blocked.
  Restrictions limited to specific platforms are not treated as hard-blocks and will still be forwarded.
- Albums are sent as a group first; on failure, it downgrades to per-message forwarding.
- If you see frequent `FloodWait`, tune `rate_limit` parameters.

### 9. Docker

```bash
docker compose up -d --build
```

Volume mounts:

- `./config.yaml` -> `/app/config.yaml`
- `./data` -> `/app/data`
- `./sessions` -> `/app/sessions`

### 10. Logs and Troubleshooting

Main loggers:

- `tg_forward_bot.handlers`
- `tg_forward_bot.link_parser`
- `tg_forward_bot.forwarder`
- `tg_forward_bot.syncer`
- `tg_forward_bot.monitor`

See `docs/operations.md` and `docs/architecture.md` for deeper operational details.

