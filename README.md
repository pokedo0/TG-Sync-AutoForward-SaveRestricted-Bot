# TG Forward Bot

[中文文档](./README.zh-CN.md)

A Telethon-based Telegram message forwarding tool with a **Bot + UserBot** dual-client architecture. Supports link parsing, historical message sync, real-time monitoring, and multi-strategy graceful degradation.

## Features

- **Link Parsing** — Send a `t.me/...` link in private chat to trigger forwarding; supports public/private channels, groups, comments, and forum topics (optional `[Source Link](url)` footer via `append_source_link`)
- **Historical Sync** — `/sync` pulls past messages with resumable progress tracking; restricted messages are automatically forwarded via Takeout inline
- **Restricted Message Export** — `/syncrestrictedmsg` re-exports restricted messages in bulk and `/takeout` resolves a single restricted post/album via Telegram's Takeout API
- **Real-Time Monitoring** — `/monitor` watches for new messages and forwards them automatically
- **Multi-UserBot Pool & Auto-Reconnect** — Configure multiple accounts under `phones`; probes private sources in priority order and auto-reconnects offline/stale clients before probing
- **High-Speed Transfer** — FastTelethon multi-connection concurrent download/upload acceleration plus `cryptg` C/Rust AES encryption
- **Graceful Degradation** — Bot direct → UserBot-assisted → download & re-upload → failure marker `#fail2forward`
- **Album-Aware** — Sends media groups as a batch first, falls back to per-message on failure
- **Hard-Block Filtering** — Automatically skips globally restricted content (`platform=all`); platform-specific restrictions are still forwarded
- **Dual Mode** — Supports both `copy` (default) and `forward` transfer modes

## Architecture

| Role        | Responsibility                                  |
| ----------- | ----------------------------------------------- |
| **Bot**     | Command handling, target-side delivery (write)  |
| **UserBot** | Restricted-source access, media download (read) |

Design principle: **UserBot reads, Bot writes** — UserBot only participates in write operations when necessary.

## Prerequisites

- Python 3.11+
- Telegram `api_id` / `api_hash` ([my.telegram.org](https://my.telegram.org))
- Bot Token ([@BotFather](https://t.me/BotFather))
- One or more UserBot accounts (phone number login)

## Getting Started

### 1. Installation & Configuration

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # Fill in api_id, api_hash, bot_token, and phone/phones
```

### 2. UserBot Login & Session Initialization

Before starting the bot, initialize UserBot sessions via the interactive login tool (supports multiple accounts):

* **Using the auto-adaptive script (Recommended on Linux / VPS)**:
  ```bash
  bash scripts/login_userbot.sh -p +8613800138000
  ```
* **Using Python directly**:
  ```bash
  python scripts/login_userbot.py -p +8613800138000
  ```
* **Using Docker interactive run**:
  ```bash
  docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000
  ```

> **Notes**:
> 1. Follow the terminal prompt to enter the SMS verification code and 2FA password. Session files are persisted in `sessions/`.
> 2. If configuring multiple UserBots (under `phones` in `config.yaml`), run the login command once for each phone number.
> 3. `.session` files are cross-platform SQLite databases; you can also log in on a local machine and copy the generated files to the server's `sessions/` directory.

### 3. Start the Bot

- **Direct run**:
  ```bash
  python main.py
  ```
  *(On Windows you can also run `start.bat`.)*
- **Linux Systemd one-click install** (creates service `forward-bot`):
  ```bash
  sudo bash install.sh
  ```
  Common service commands:
  ```bash
  systemctl status forward-bot     # Check status
  journalctl -u forward-bot -f     # Follow live logs
  systemctl stop forward-bot       # Stop service
  systemctl restart forward-bot    # Restart service
  ```

## Key Configuration (`config.yaml`)

See [`config.example.yaml`](config.example.yaml) for full comments:

- **Multi-UserBot (`phones`)**: Ordered list of phone numbers (backward compatible with single `phone`). Private chats/channels are probed top-to-bottom; the first accessible account is used, with automatic reconnect if a client is disconnected or stale.
- **Transfer Acceleration (`transfer`)**: Controls FastTelethon concurrent connections (`fast_download_connections`, `fast_upload_connections`, `fast_transfer_min_size_mb`, `album_download_concurrency`, `upload_part_size_kb`, `download_part_size_kb`) alongside `cryptg`.
- **Link Resolution Toggles**: `allow_public_resolve` allows non-admins to resolve links in private chat; `append_source_link` appends `[Source Link](url)` to resolved messages.

## Commands

| Command                       | Description                                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| `/sync <link> [--forward]`    | Sync historical messages to the current chat; restricted messages are forwarded via Takeout inline |
| `/syncrestrictedmsg <link>`   | Re-export only restricted messages to the current chat via Takeout                                 |
| `/takeout <link>`             | Resolve and export a single restricted message or album via Takeout (also works by replying)       |
| `/monitor <link> [--forward]` | Monitor new messages and forward to the current chat (UserBot must have joined the source)         |
| `/list`                       | Task management: pause / resume / delete / clear                                                   |
| `/settings`                   | View rate-limit configuration                                                                      |
| `/start` · `/help`            | Startup guide and help                                                                             |

Sending a `t.me/...` link in a private chat with the bot triggers parsing and forwarding.

## Source Access Requirements

| Source Type             | UserBot for `/sync`                | UserBot for `/monitor`                | UserBot for `/syncrestrictedmsg` & `/takeout` |
| ----------------------- | ---------------------------------- | ------------------------------------- | --------------------------------------------- |
| Public channel / group  | Usually no membership required     | **Must be a member**                  | Must be a member (Takeout requires it)        |
| Private channel / group | Must be a member with read access  | **Must be a member with read access** | Must be a member with read access             |
| Forum topic             | Must have read access to the group | **Must be a member**                  | Must have read access to the group            |

- `/monitor` validates UserBot membership and access before creating a task; the task is rejected if requirements are not met.
- `/syncrestrictedmsg` and `/takeout` use Telegram's Takeout API, which requires UserBot to be a member of the source chat. `/syncrestrictedmsg` scans all messages first, then opens a Takeout session to re-export only the restricted ones.
- Bot always handles target-side delivery; source-side readability depends on whether the source is public.

## Forwarding Strategy

The runtime always attempts strategies in order, falling back automatically on failure:

1. **Bot Direct** — Lowest cost; preferred when Bot can read the source
2. **UserBot-Assisted** — UserBot reads the source, Bot delivers
3. **Download & Re-upload** — UserBot downloads media, Bot re-uploads (used when content is protected or forwarding is restricted)
4. **Failure Marker** — Sends `#fail2forward` when all strategies are exhausted

> **Note:** "Strategy 1 succeeded" in logs means the first-tier attempt succeeded — in `copy` mode this may internally use `send_message` / `send_file`. Comment links (`?comment=`) point to the linked discussion group, which may be private even if the original post is public.

## Docker Deployment

```bash
# 1. Copy and configure settings
cp config.example.yaml config.yaml

# 2. Interactively initialize UserBot session (Required on first run)
docker compose run --rm bot python scripts/login_userbot.py -p +8613800138000

# 3. Build and start the bot service in the background
docker compose up -d --build

# 4. View logs / stop service
docker compose logs -f bot
docker compose stop
```

Volume mounts:

| Host            | Container          |
| --------------- | ------------------ |
| `./config.yaml` | `/app/config.yaml` |
| `./data`        | `/app/data`        |
| `./sessions`    | `/app/sessions`    |

## Important Notes

- **Restricted messages are sent as UserBot**: Takeout media references are bound to UserBot; Bot cannot use them directly.
- `forward` mode preserves native forwarding semantics; `copy` mode is more compatible but may trigger rate limits faster
- For private sources, UserBot must be a member with read access — otherwise strategies 2 and 3 will both fail
- Forum topic forwarding relies on `target_topic_id`; insufficient permissions on the target side will cause delivery failures
- If you encounter frequent `FloodWait` errors, increase the `rate_limit` parameters in your config
- Albums are sent as a group first; on failure, the bot falls back to per-message forwarding

## Logging & Troubleshooting

Logs are written to stdout/console (use `journalctl -u forward-bot -f` for systemd or `docker compose logs -f bot` for Docker).

| Logger                             | Purpose                                                |
| ---------------------------------- | ------------------------------------------------------ |
| `tg_forward_bot.handlers`          | Command and private-chat parsing entry point           |
| `tg_forward_bot.userbot_manager`   | Multi-UserBot probing, routing, and auto-reconnection  |
| `tg_forward_bot.link_parser`       | Link resolution and discussion group discovery         |
| `tg_forward_bot.forwarder`         | Strategy execution and fallback                        |
| `tg_forward_bot.media_transfer`    | Media download/upload orchestration and fallback       |
| `tg_forward_bot.fast_telethon`     | FastTelethon multi-connection concurrent transfer      |
| `tg_forward_bot.syncer`            | Historical sync progress                               |
| `tg_forward_bot.restricted_syncer` | Restricted message Takeout sync                        |
| `tg_forward_bot.monitor`           | Real-time monitoring events                            |

For detailed operational guidance, see [`docs/operations.md`](docs/operations.md) and [`docs/architecture.md`](docs/architecture.md).
