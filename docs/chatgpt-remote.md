# ChatGPT Remote MCP Setup

ChatGPT cannot use a GitHub repository URL directly as an MCP server. GitHub can host this code, but ChatGPT needs a running remote MCP endpoint over SSE or streamable HTTP.

## Safe publishing checklist

Before pushing to GitHub:

- Keep the repository private unless you intentionally want the code public.
- Do not commit `.env`, `.env.local`, app-specific passwords, Apple ID emails, tokens, logs, or local virtualenvs.
- Commit `.env.example` only with placeholder values.
- Use GitHub secret scanning if the repo is public or shared with others.

Before deploying:

- Store `ICLOUD_USERNAME`, `ICLOUD_APP_PASSWORD`, `ICLOUD_USER_ALIASES`, and `ICLOUD_USER_TIMEZONE` as hosting-provider environment variables or secrets.
- Start with `ICLOUD_MCP_READ_ONLY=1` until you have verified that ChatGPT is calling only the tools you expect.
- Do not expose this server publicly without access control. It can read and modify your calendar and mail.

## HTTP transport

The default local transport is `stdio`, for Claude Desktop and other local MCP clients.

For a remote ChatGPT connector, run with streamable HTTP:

```sh
MCP_TRANSPORT=streamable-http \
MCP_HOST=0.0.0.0 \
MCP_PORT=8000 \
MCP_ALLOWED_HOSTS=your-deployment-host.example.com \
uv run icloud-mcp
```

The MCP endpoint is:

```text
https://your-deployment-host.example.com/mcp
```

SSE is also available:

```sh
MCP_TRANSPORT=sse \
MCP_HOST=0.0.0.0 \
MCP_PORT=8000 \
MCP_ALLOWED_HOSTS=your-deployment-host.example.com \
uv run icloud-mcp
```

The SSE endpoint is:

```text
https://your-deployment-host.example.com/sse
```

## ChatGPT

In ChatGPT, enable developer mode, then add the deployed MCP URL in Settings > Connectors. ChatGPT currently supports remote MCP servers over SSE and streamable HTTP.

For this specific server, prefer streamable HTTP and read-only mode first.
