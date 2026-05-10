# icloud-mcp

A Model Context Protocol server that connects an MCP client to your iCloud Calendar and Mail. Runs locally by default; credentials stay in your environment or deployment secrets.

**24 tools** across calendar (9), mail (13), and cross-cutting workflows (2). All communication is between your Mac and iCloud directly.

> ChatGPT needs a running remote MCP endpoint, not a GitHub repository URL. See [docs/chatgpt-remote.md](docs/chatgpt-remote.md) before deploying this server remotely.

## What this can do

After installing, ask your MCP client things like:

- *"What's on my calendar tomorrow?"*
- *"Find a 45-minute slot Wednesday afternoon."*
- *"Decline the all-hands on Friday."*
- *"What unread mail do I have from this week?"*
- *"Walk me through my 10am — show recent emails with the attendees."* (uses `prep_for_meeting`)
- *"Give me a brief for today."* (uses `today_brief`)
- *"Reply to the latest email from Alice."* (preview with `dry_run=true`, then confirm before sending)
- *"Move all newsletters from this week to Archive."*

## Setup

### 1. Get an iCloud app-specific password (one-time)

Go to [appleid.apple.com](https://appleid.apple.com) → **Sign-In and Security** → **App-Specific Passwords** → **Generate Password**. Label it "iCloud MCP". Copy the 16-character password — you won't see it again. The same password works for Calendar, Mail, and SMTP.

### 2. Install [`uv`](https://docs.astral.sh/uv/)

```sh
brew install uv
```

The MCP runs Python under `uv`, which manages the virtualenv and dependencies for you.

### 3. Configure a local MCP client

Unzip the project somewhere stable (e.g. `~/code/icloud-mcp`), then add a server entry to your MCP client's configuration:

```json
{
  "mcpServers": {
    "icloud": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/Users/YOU/code/icloud-mcp",
        "python", "-m", "icloud_mcp.server"
      ],
      "env": {
        "ICLOUD_USERNAME": "you@icloud.com",
        "ICLOUD_APP_PASSWORD": "xxxx-xxxx-xxxx-xxxx",
        "ICLOUD_USER_ALIASES": "",
        "ICLOUD_USER_TIMEZONE": "America/Los_Angeles",
        "ICLOUD_MCP_READ_ONLY": "0"
      }
    }
  }
}
```

Use the absolute path; no `~`. If your MCP client can't find `uv`, replace `"uv"` with the output of `which uv`.

### 4. Run the pre-flight check

This validates everything end-to-end without involving an MCP client. Six checks: env, CalDAV auth, calendar read, IMAP auth, mail read, SMTP auth. Sends nothing.

```sh
cd icloud-mcp
cp .env.example .env
# Edit .env with your credentials
uv run python scripts/preflight.py
```

If all six pass, the system is ready. If something fails, the message tells you exactly what to fix.

### 5. Verify the MCP connection

Restart your MCP client. In a new chat, ask:

> *"What's on my calendar tomorrow?"*

The client should call `list_events` and answer.

## Optional configuration

| Variable | Default | Purpose |
|---|---|---|
| `ICLOUD_USER_ALIASES` | empty | Comma-separated additional emails you receive mail/invites under (`you@me.com,you@mac.com`). Without this, `respond_to_invite` may fail when the invite was addressed to your alias. |
| `ICLOUD_USER_TIMEZONE` | `UTC` | IANA name like `America/Los_Angeles`. Used by `today_brief` so "today" matches your local time. |
| `ICLOUD_MCP_READ_ONLY` | `0` | Set to `1` to disable all writes (sends, deletes, edits). Reads still work. |

## Tools

### Calendar (9)

**Reads:** `list_calendars`, `list_events`, `get_event`, `search_events`, `find_free_slots`.

**Writes** (`dry_run` available): `create_event`, `update_event`, `delete_event`, `respond_to_invite`.

`update_event` and `delete_event` support per-occurrence operations on recurring events: pass `occurrence_start` to target a single instance, omit to target the whole series.

`find_free_slots` correctly excludes cancelled events, transparent events ("free time"), and invites you've declined. Window capped at 90 days.

### Mail (13)

**Reads:** `list_mailboxes`, `list_messages`, `get_message`, `search_mail`, `get_thread`, `get_attachment`.

**Writes** (`dry_run` on `send_mail`): `send_mail`, `mark_read`, `mark_unread`, `flag_messages`, `unflag_messages`, `move_message`, `delete_message`.

`get_thread` accepts `additional_mailboxes=["Sent Messages"]` to span incoming + outgoing. `delete_message` defaults to soft-delete (move to Trash); `permanent=true` for hard-delete.

`send_mail` saves to Sent Messages via IMAP APPEND (iCloud SMTP doesn't auto-save). For replies, fetch the parent first and pass `in_reply_to=parent.message_id` plus `references=parent.references + [parent.message_id]`.

### Workflows (2)

- **`today_brief`** — Today's events + cancelled count + pending invites + recent unread mail in one call. Honors `ICLOUD_USER_TIMEZONE`.
- **`prep_for_meeting`** — Event details + bidirectional email correspondence with each attendee (last 14 days by default), searched across INBOX *and* Sent Messages, deduplicated by Message-ID. Filters out the user's primary email and aliases from the attendee list.

## Behavioral details worth knowing

**Recurring events.** Updates and deletes have two modes: omit `occurrence_start` to target the whole series, provide it (the `recurrence_id` from `list_events`) to target a single instance. The default in `update_event` is series-wide, so be explicit when you only want to change one occurrence.

**Sending mail.** Always preview with `dry_run=true` first. The response includes the rendered RFC822 message; review the subject, recipients, and body before re-running with `dry_run=false`. SMTP send and Sent-folder save are separate operations: if SMTP succeeds but Sent-save fails, you get `sent=true, saved_to_sent=false, warning="..."` rather than a misleading total failure.

**Search semantics.** `search_mail` requires at least one content filter (`query`, `from_addr`, `to_addr`, or `subject`) — for pure date browsing use `list_messages`. `search_events` does substring matching client-side after fetching the window.

**Thread reconstruction.** `get_thread` walks Message-ID, In-Reply-To, and References headers. By default it searches only the seed's mailbox; pass `additional_mailboxes=["Sent Messages"]` for full bidirectional context.

**Read-only mode.** Set `ICLOUD_MCP_READ_ONLY=1` in env. All write tools refuse with `ReadOnlyError`. Useful while you're getting comfortable.

**Aliases.** Apple sometimes delivers invites and mail to your `@me.com` or `@mac.com` aliases even though you connected with `@icloud.com`. Set `ICLOUD_USER_ALIASES` to a comma-separated list so `respond_to_invite` finds your attendee record and `prep_for_meeting` correctly filters you out of attendee lists.

## Troubleshooting

**Pre-flight fails on auth.** Username must be the full email (`@icloud.com`/`@me.com`/`@mac.com`). Password must be the 16-char app-specific one, not your Apple ID password. Generate a fresh one if in doubt.

**`ConflictError` on update/delete.** You fetched the event, then someone else (you on iPhone Calendar?) modified it before your write reached iCloud. Refetch with `get_event` and retry.

**`respond_to_invite` says "user not in attendee list".** Apple delivered the invite to an alias. Set `ICLOUD_USER_ALIASES=you@me.com,you@mac.com` and try again.

**Empty event list when you expect events.** `start` and `end` must include timezone offsets. Naive datetimes are rejected.

**Your MCP client doesn't see the server.** Most common causes are a wrong directory path in config, missing environment variables, or `uv` not being on the client's PATH. Replace `"uv"` with the full output of `which uv`.

## Project layout

```
icloud-mcp/
├── manifest.json            # MCPB manifest for one-click install
├── pyproject.toml           # uv-managed deps
├── src/icloud_mcp/
│   ├── server.py            FastMCP entrypoint, shared client construction
│   ├── config.py            env loading + validation (incl. aliases, timezone)
│   ├── errors.py            domain error types
│   ├── util.py              shared helpers (validate_email)
│   ├── calendar/            CalDAV: client, models, tools
│   ├── mail/                IMAP + SMTP: client, models, tools
│   └── workflows/           Cross-cutting tools (today_brief, prep_for_meeting)
├── scripts/
│   ├── preflight.py         End-to-end pre-flight check (recommended)
│   ├── smoke.py             Calendar smoke test
│   └── smoke_mail.py        Mail smoke test
└── tests/                   119 unit tests
```

## Remote transports

The default transport is local `stdio`. For remote MCP clients, set `MCP_TRANSPORT=streamable-http` or `MCP_TRANSPORT=sse`; see [docs/chatgpt-remote.md](docs/chatgpt-remote.md).
