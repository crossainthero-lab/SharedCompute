# Connecting agents across separate networks

Provider agents always open the connection (outbound HTTPS) and send a per-node bearer token. Provider
machines therefore need no port forwarding, and home routers and NAT are not a problem. What must be
reachable is the **marketplace**, and it must be reachable over TLS.

## Rules the code enforces

- **Agent:** refuses `http://` for anything but `127.0.0.1` / `localhost` / `::1`. It verifies TLS
  certificates against the system trust store, or against a private CA given with `--ca-file`, and requires
  TLS 1.2 or newer. It warns if the token file is readable by other users.
- **Server:** `serve` refuses a non-loopback `--host` unless given `--tls-cert/--tls-key` or
  `--behind-tls-proxy`. An exposed server (non-loopback, or behind a proxy):
  - marks session cookies `Secure`;
  - closes self-registration (create accounts with `python -m sharedcompute add-user`);
  - requires sign-in to browse the marketplace;
  - hides `/api/docs`.

  Each can be re-enabled explicitly with `SHAREDCOMPUTE_REGISTRATION=open`, `SHAREDCOMPUTE_PUBLIC_MARKETPLACE=1`
  and `SHAREDCOMPUTE_API_DOCS=1`.
- **Never** run `serve --host 0.0.0.0` on a machine with a public IP without TLS. The code refuses it, and it
  should never be worked around.

## Option A: Tailscale (recommended first pilot, free, nothing public)

Both machines join a private tailnet, so the marketplace is never on the public internet. Tailscale provides
HTTPS certificates for `*.ts.net` names, so agents need no `--ca-file`.

```bash
# Marketplace machine (e.g. the Mac), after installing Tailscale and enabling HTTPS certificates
# in the tailnet admin console:
python -m sharedcompute add-user you@example.com --name William --operator
python -m sharedcompute serve --behind-tls-proxy          # binds 127.0.0.1:8765
tailscale serve --bg 8765                                 # HTTPS on https://<machine>.<tailnet>.ts.net
                                                          # (syntax varies by version: tailscale serve --help)

# GPU machine (also on the tailnet):
python -m sharedcompute_agent run --server https://<machine>.<tailnet>.ts.net \
  --token-file ~/.sharedcompute-node-token --gpu 0
```

Restrict who can reach the marketplace with tailnet ACLs. Each provider joins your tailnet, or you share the
single machine with them.

## Option B: Private CA (same LAN, or any network you control)

```bash
scripts/make-dev-tls.sh ~/sc-tls market.lan 192.168.1.20   # names/IPs the agent will use
python -m sharedcompute serve --host 0.0.0.0 --tls-cert ~/sc-tls/server.crt --tls-key ~/sc-tls/server.key
# agents: --server https://192.168.1.20:8765 --ca-file ca.crt
```

`ca.crt` goes to every agent and browser that should trust the marketplace. `ca.key` stays offline. The
automated test suite exercises exactly this setup on loopback (agent ↔ server over TLS with a private CA, and
refusal of an untrusted certificate).

## Option C: Small VPS with a domain (closest to production)

Run the marketplace on a cheap VPS behind Caddy, which obtains Let's Encrypt certificates automatically:

```
# /etc/caddy/Caddyfile
market.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

```bash
SHAREDCOMPUTE_TRUSTED_PROXIES=127.0.0.1 python -m sharedcompute serve --behind-tls-proxy
```

Agents use `--server https://market.example.com` with no `--ca-file`. Before anyone outside the founders uses it,
work through the "before real users" list in the README.

## What is not built yet

- Mutual TLS (client certificates) or token binding for agents. Tokens are bearer secrets, so rotate them
  from the Provide page if one leaks.
- Rate limiting on agent endpoints.
- A push channel (WebSocket/gRPC). Agents poll every 2 s and heartbeat every 10 s.
- Signed agent releases and auto-update.
