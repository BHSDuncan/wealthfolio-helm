# Optional MCP access

MCP is disabled by default and uses the application's `/mcp` endpoint. It requires password authentication **and a scoped personal access token**; browser passwords and session cookies are not MCP bearer credentials. No AI-provider account or API key is required to enable the endpoint.

## Enable and connect

Apply [the MCP example](../examples/mcp.yaml) alongside your existing authentication settings. Replace its example hosts with the intended browser/client hostnames and enable MCP. The allowlist matches HTTP Host values: names are case-insensitive, a hostname without a port accepts any port, and an explicit port must match. Never use `*`. Your Ingress must preserve Host and support streaming/SSE with a suitable request timeout.

In **Settings → AI Agent Access**, create a named, expiring token with only the required scopes. Start with `accounts:read`, store the once-visible raw token securely, and configure your MCP client to use it as a bearer token at your installation's `/mcp` URL. Revoke it when finished.

Other read scopes include `holdings:read`, `performance:read`, `activities:read`, `financial-planning:read`, `health:read` and `classification:read`. Tokens are profile-bound: changing the selected browser profile does not change a token's binding. Optional `X-WF-Profile-Id` requests must respect that binding.

## Audit and troubleshooting

Audit is enabled by default and stored with the profile data. Review it under Settings → AI Agent Access and protect it with your complete data backup; events can include tool names, outcomes and identifiers. Set `mcp.auditEnabled: false` only if you deliberately want to disable audit.

An unlisted Host is rejected even with a valid token. Check token expiry, revocation, scopes, profile binding and the Host allowlist when access fails. In Wealthfolio 3.11.0, a POST to disabled `/mcp` can return 405; this does not mean MCP is enabled. Confirm the application's reported MCP status. See [upstream agent architecture](https://github.com/wealthfolio/wealthfolio/blob/d1fda0b65f69d8557b1ef80e555c799215ad28b3/docs/architecture/cli-mcp-agent-access.md) for protocol details.
