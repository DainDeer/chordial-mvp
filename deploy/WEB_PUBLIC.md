# taking the focus view public: cloudflared + chordial login

the web focus view stays bound to `127.0.0.1` forever. a cloudflare tunnel
(`cloudflared`) dials OUT from the server to cloudflare's edge and serves
`https://focus.<your-domain>` from there — no open ports, no certificates to
manage, no nginx. auth is chordial itself: you ask the bot to log you in,
it DMs a one-time code + tap-to-login link (the `web_login` tool), and
redeeming it mints a signed session cookie. localhost use (no
`WEB_PUBLIC_URL`) is unchanged and needs none of this.

## one-time setup (on the server)

1. install cloudflared (debian/ubuntu):

   ```
   curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
   echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
   sudo apt-get update && sudo apt-get install cloudflared
   ```

2. authenticate against the domain (opens a browser url; pick the zone):

   ```
   cloudflared tunnel login
   ```

3. create the tunnel and route the subdomain (this also creates the DNS
   record in cloudflare — no dashboard visit needed):

   ```
   cloudflared tunnel create chordial-focus
   cloudflared tunnel route dns chordial-focus focus.<your-domain>
   ```

4. `~/.cloudflared/config.yml`:

   ```yaml
   tunnel: chordial-focus
   credentials-file: /home/dain/.cloudflared/<tunnel-uuid>.json
   ingress:
     - hostname: focus.<your-domain>
       service: http://localhost:8484
     - service: http_status:404
   ```

5. run it as a service:

   ```
   sudo cloudflared service install
   sudo systemctl enable --now cloudflared
   ```

## chordial config (prod .env)

```
WEB_PUBLIC_URL=https://focus.<your-domain>
WEB_SESSION_SECRET=<openssl rand -hex 32>
```

`WEB_PUBLIC_URL` is the ONE switch: it turns on required sessions, the
/login page, Secure cookies, and registers the `web_login` tool so the bot
can hand out codes. the service refuses to start public without a session
secret. then migrate (adds `link_codes.purpose`) and restart:

```
poetry run alembic upgrade head
sudo systemctl restart chordial
```

## embedding in the portfolio desktop (focus.exe)

the portfolio site shows the focus view inside an iframe window. every
response carries `Content-Security-Policy: frame-ancestors ...`, and the
default is `'self'` — nobody else may frame the page (clickjacking
protection). to let the portfolio embed it, widen the list in prod `.env`:

```
WEB_FRAME_ANCESTORS="'self' https://internetcreature.dev"
```

(the double quotes matter: dotenv strips them and keeps the inner `'self'`,
which is CSP syntax. an unquoted line doesn't parse and silently leaves the
default in place.)

the subdomains are same-*site*, so the SameSite=Lax session cookie flows
inside the iframe with no further changes; the page detects it's framed and
switches to a compact embed layout on its own.

## the dev daemon

both daemons default to port 8484; `.env.dev` moves dev to 8485 so they can
coexist. keep `WEB_PUBLIC_URL` OUT of `.env.dev` unless you also create a
second tunnel hostname — the dev sandbox stays localhost-only by default.

## notes

- sessions are stateless HMAC cookies (30 days, `WEB_SESSION_DAYS`).
  rotating `WEB_SESSION_SECRET` logs everyone out — that IS the revocation
  story at this scale.
- the login link (`/login?code=...`) only renders a page; the code is spent
  by the page's script POSTing it. telegram's link-preview fetcher therefore
  can't burn the code before you tap it.
- code redemption is rate-limited per client ip using cloudflare's
  `CF-Connecting-IP` header, which is trustworthy here because the ONLY
  route to the port is the tunnel (loopback bind).
