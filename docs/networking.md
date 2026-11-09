# Multiple computers, one configured resource view

A Sheetbend gateway can be another Sheetbend registry's source. This is explicit
composition, not peer discovery, automatic credential synchronization, failover,
or a distributed scheduler. Each host owns its own TOML, credentials, and ledger.
Only named and authorized routes are visible through a gateway.

## Recommended first deployment: SSH forwarding

On the Studio, configure and start the gateway from the [quickstart](studio-quickstart.md).
Give the laptop its own random gateway client token and grant only the routes it
needs. Keep the oMLX key on the Studio. Transfer the client token file through your
existing secure credential workflow into an owner-only file on the laptop.

On the laptop, with your normal SSH host alias and public-key authentication:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18765:127.0.0.1:8765 studio
```

SSH authenticates/encrypts the connection; the gateway token authorizes API routes.
The SSH public key is not itself an HTTP bearer token. No SSH agent forwarding,
key generation, tunnel management, or key exchange is implemented by Sheetbend.
`ExitOnForwardFailure` checks tunnel setup, not later model availability.
See the [OpenSSH forwarding documentation](https://man.openbsd.org/ssh_config#LocalForward).

Register the forwarded gateway as a source:

```toml
[credentials.studio-access]
provider = "file"
path = "secrets/studio-access.txt"

[sources.studio-peer]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:18765/v1"
default_model = "studio-chat"
scope = "external"
auth = { type = "bearer", credential = "studio-access" }

[sources.studio-peer.models.studio-chat]
capabilities = { chat = true, streaming = true }
```

`studio-chat` is the alias exported by the Studio gateway, not the Qwen repository
name. Declare only capabilities the peer route actually provides. A successful peer
catalog confirms that the gateway permits/listed the route, not that its underlying
model is loaded or working. Use an explicit inference probe for that distinction.

**Loopback transport does not make remote computation local.** In schema 2,
`local` means the executing Python process's machine. Use `institutional` plus the
correct organization for an approved institutional boundary, or `external` for a
personal remote machine outside that boundary. `external` does not imply a public
Internet server; it means outside those other declared boundaries. Accordingly:

```bash
sheetbend check studio-peer --allow-external
sheetbend dashboard --probe --allow-external
sheetbend run --source studio-peer --allow-external --adapter openai -- python agent.py
```

Python callers likewise include `external` explicitly in `allowed_scopes`.
A peer gateway that can forward onward must be declared according to its actual
permitted processing boundary, not just where its TCP listener sits. These are
operator declarations, not proof of peer behavior or institutional approval.

## Aggregate local and remote resources

[examples/federated-gateway.toml](../examples/federated-gateway.toml) is a complete
configuration skeleton. It declares a local laptop model and a Studio peer, then
exports two aliases from the laptop's own gateway:

```toml
[gateway]
allowed_scopes = ["local", "external"]

[gateway.routes.laptop-chat]
source = "laptop"
model = "YOUR_LAPTOP_MODEL_ID"

[gateway.routes.studio-chat]
source = "studio-peer"
model = "studio-chat"

[gateway.clients.application]
credential = "application"
routes = ["laptop-chat", "studio-chat"]
```

The application sees one `/v1/models` catalog containing both aliases. Selecting an
alias selects its fixed route. No model moves between computers, and no source is
silently substituted when a machine is asleep. The dashboard displays both the
configured sources/models and their export routes and clients, including offline
or inaccessible entries rather than hiding them.

Avoid cycles. A forwarded `X-Sheetbend-Hop` counter rejects requests at four hops;
admission and whole-request deadlines also bound failures. This is loop protection,
not a consensus protocol or an end-to-end authenticated provenance chain.

## LAN or public HTTPS

Keep Sheetbend on loopback. Put a maintained TLS reverse proxy in front of it,
with normal certificate renewal, firewalling, request limits, and optionally client
certificates or a private overlay network. Do not expose an unauthenticated raw
model server or send bearer tokens over public HTTP. The client-facing Sheetbend
source should use an explicit `https://.../v1` URL with certificate verification.

For an already configured HTTPS NGINX server, the location fragment is:

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8765;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_next_upstream off;
    proxy_read_timeout 300s;
    client_max_body_size 16m;
}
```

This is not a full TLS deployment configuration. The explicit loopback Host header
is required by Sheetbend. No browser Origin header is supported. Forwarded address
headers do not establish Sheetbend client identity. Preserve the client's gateway
bearer token, disable response buffering for SSE, and do not configure proxy retries
of generation requests. Avoid logging Authorization or request/response bodies.
See [NGINX proxy directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

## Rotation and operations

Use separate gateway tokens per computer/application and narrow route grants.
Remove a grant or rotate its referenced token, then restart that gateway. Persistent
tokens do not have automatic expiry in this version. Upstream credential rotation
also requires gateway restart. For services, configure the approved credential
context in the service manager; do not assume Finder or a LaunchAgent inherits an
interactive shell's environment or unlocked Keychain state.

The Studio's limiter coordinates calls that reach that Studio's Sheetbend gateway
under the same OS user and config path. Laptop-side limits are additional limits,
not a distributed replacement for Studio or provider limits. There is no fleet-wide
`top` aggregation, health-based routing, automatic token copying, or shared SQLite
file across computers. Keep runtime SQLite on each host's local filesystem.
