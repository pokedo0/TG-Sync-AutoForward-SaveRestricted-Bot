# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `/syncrestrictedmsg` command to sync restricted messages from channels that block forwarding, using Telegram's Takeout API for authorized bulk export.

### Changed
- Refactored core components with a shared `BaseComponent` base class for consistent logging and client access across syncer, forwarder, and restricted syncer.
- Restructured syncer internals for better separation of concerns and maintainability.

### Fixed
- Improved forum topic forwarding reliability by correctly handling the Telegram "General" topic (`id=1`) matching and synchronization behavior.

## [0.1.0] - 2026-03-17

### Added
- Initial Telegram forwarding bot with `Bot + UserBot` dual-client architecture.
- Private-link parsing for channels, groups, comments, and forum topics.
- `/sync` command for historical message backfill.
- `/monitor` command for real-time forwarding task creation.
- Multi-strategy forwarding engine with automatic degradation and recovery across direct send, user-assisted forwarding, and re-upload paths.
- Album-aware forwarding with grouped-send first and per-message fallback.
- Task management commands for listing and operating running tasks.
- Rate-limit settings view and operational controls.
- Failure marker output (`#fail2forward`) when all forwarding strategies fail.

### Changed
- Strengthened `/monitor` source-access validation to enforce UserBot readability before creating monitor tasks.
- Refined task information and operator-facing monitoring feedback.
- Improved hard-block filtering behavior for cross-platform restrictions.
- Improved reply-thread handling during forwarding and synchronization.
- Added tooling support for link-response diagnostics.
- Updated dependency/install workflow and service naming in deployment scripts.

### Fixed
- Resolved Telethon compatibility issue with `GetForumTopicsByIDRequest` imports (Telethon 1.42+).
- Restored strategy fallback behavior for message entity parsing failures.
- Fixed video-cover handling in degraded forwarding paths.
