# Optional MCP access

Model Context Protocol (MCP) access is disabled by default. It lets an MCP client connect to Wealthfolio's `/mcp` endpoint using a scoped personal access token (PAT). The application's password authentication must remain enabled, but the client sends the PAT as its bearer credential, not a browser password or session cookie. No AI-provider account or API key is needed to enable the endpoint.

## Enable and connect

For a standard Helm installation, copy [the MCP example](../examples/mcp.yaml), replace its hosts with the intended client-facing hostnames, and pass it as an additional values file alongside your existing configuration. The example enables MCP. For an installer-managed installation, review the [configuration limits](install.md#configuration-while-using-the-guided-installer) before making Helm changes.

The allowlist matches the HTTP `Host` header: names are case-insensitive, a hostname without a port accepts any port, and an explicit port must match. Wildcard `*` is not allowed. Your ingress controller or proxy must preserve Host and support streaming/server-sent events (SSE) with a suitable request timeout.

In **Settings → AI Agent Access**, create a named, expiring token with only the required scopes. Start with `accounts:read`, store the once-visible raw token securely, and configure your MCP client to use it as a bearer token at your installation's `/mcp` URL. Revoke it when finished.

Other read scopes include `holdings:read`, `performance:read`, `activities:read`, `financial-planning:read`, `health:read` and `classification:read`. Tokens are profile-bound: changing the selected browser profile does not change a token's binding. Optional `X-WF-Profile-Id` requests must respect that binding.

## Audit and troubleshooting

Audit is enabled by default and stored with the profile data. Review it under Settings → AI Agent Access and protect it with your complete data backup; events can include tool names, outcomes and identifiers. Set `mcp.auditEnabled: false` only if you deliberately want to disable audit.

An unlisted Host is rejected even with a valid token. Check token expiry, revocation, scopes, profile binding and the Host allowlist when access fails. In Wealthfolio 3.11.0, a POST to disabled `/mcp` can return 405; this does not mean MCP is enabled. Confirm the application's reported MCP status. See [upstream agent architecture](https://github.com/wealthfolio/wealthfolio/blob/d1fda0b65f69d8557b1ef80e555c799215ad28b3/docs/architecture/cli-mcp-agent-access.md) for protocol details.
