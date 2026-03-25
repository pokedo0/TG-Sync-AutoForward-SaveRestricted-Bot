# TG Forward Bot

[中文文档](./README.zh-CN.md)

A Telethon-based Telegram message forwarding tool with a **Bot + UserBot** dual-client architecture. Supports link parsing, historical message sync, real-time monitoring, and multi-strategy graceful degradation.

## Features

- **Link Parsing** — Send a `t.me/...` link in private chat to trigger forwarding; supports public/private channels, groups, comments, and forum topics
- **Historical Sync** — `/sync` pulls past messages with resumable progress tracking; restricted messages are automatically forwarded via Takeout inline
- **Restricted Message Sync** — `/syncrestrictedmsg` re-exports only platform-restricted messages using Telegram's Takeout API
- **Real-Time Monitoring** — `/monitor` watches for new messages and forwards them automatically
- **Graceful Degradation** — Bot direct → UserBot-assisted → download & re-upload → failure marker `#fail2forward`
- **Album-Aware** — Sends media groups as a batch first, falls back to per-message on failure
- **Hard-Block Filtering** — Automatically skips globally restricted content (`platform=all`); platform-specific restrictions are still forwarded
- **Dual Mode** — Supports both `copy` (default) and `forward` transfer modes

## Architecture

| Role | Responsibility |
|------|----------------|
| **Bot** | Command handling, target-side delivery (write) |
| **UserBot** | Restricted-source access, media download (read) |

Design principle: **UserBot reads, Bot writes** — UserBot only participates in write operations when necessary.

## Prerequisites

- Python 3.11+
- Telegram `api_id` / `api_hash` ([my.telegram.org](https://my.telegram.org))
- Bot Token ([@BotFather](https://t.me/BotFather))
- A UserBot account (phone number login)

## Getting Started

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # Fill in your credentials
python main.py
```

> On first launch, UserBot phone verification is required. Session files are persisted in `sessions/`.

## Commands

| Command | Description |
|---------|-------------|
| `/sync <link> [--forward]` | Sync historical messages to the current chat; restricted messages are forwarded via Takeout inline |
| `/syncrestrictedmsg <link>` | Re-export only restricted messages to the current chat via Takeout |
| `/monitor <link> [--forward]` | Monitor new messages and forward to the current chat (UserBot must have joined the source) |
| `/list` | Task management: pause / resume / delete / clear |
| `/settings` | View rate-limit configuration |
| `/start` · `/help` | Startup guide and help |

Sending a `t.me/...` link in a private chat with the bot triggers parsing and forwarding.

## Source Access Requirements

| Source Type | UserBot for `/sync` | UserBot for `/monitor` | UserBot for `/syncrestrictedmsg` |
|-------------|---------------------|------------------------|----------------------------------|
| Public channel / group | Usually no membership required | **Must be a member** | Must be a member (Takeout requires it) |
| Private channel / group | Must be a member with read access | **Must be a member with read access** | Must be a member with read access |
| Forum topic | Must have read access to the group | **Must be a member** | Must have read access to the group |

- `/monitor` validates UserBot membership and access before creating a task; the task is rejected if requirements are not met.
- `/syncrestrictedmsg` uses Telegram's Takeout API, which requires UserBot to be a member of the source chat. It scans all messages first, then opens a Takeout session to re-export only the restricted ones.
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
docker compose up -d --build
```

Volume mounts:

| Host | Container |
|------|-----------|
| `./config.yaml` | `/app/config.yaml` |
| `./data` | `/app/data` |
| `./sessions` | `/app/sessions` |

## Important Notes

- `forward` mode preserves native forwarding semantics; `copy` mode is more compatible but may trigger rate limits faster
- For private sources, UserBot must be a member with read access — otherwise strategies 2 and 3 will both fail
- Forum topic forwarding relies on `target_topic_id`; insufficient permissions on the target side will cause delivery failures
- If you encounter frequent `FloodWait` errors, increase the `rate_limit` parameters in your config
- Albums are sent as a group first; on failure, the bot falls back to per-message forwarding

## Logging & Troubleshooting

| Logger | Purpose |
|--------|---------|
| `tg_forward_bot.handlers` | Command and private-chat parsing entry point |
| `tg_forward_bot.link_parser` | Link resolution and discussion group discovery |
| `tg_forward_bot.forwarder` | Strategy execution and fallback |
| `tg_forward_bot.syncer` | Historical sync progress |
| `tg_forward_bot.restricted_syncer` | Restricted message Takeout sync |
| `tg_forward_bot.monitor` | Real-time monitoring events |

For detailed operational guidance, see [`docs/operations.md`](docs/operations.md) and [`docs/architecture.md`](docs/architecture.md).
