# Deploying on Ubuntu 26.04

The stack is self-contained: **TimescaleDB + Prometheus + Grafana + Traefik +
the cron monitor**, all in one compose file. Nothing here connects to the
existing aegis_monitor instance.

That means the box owns its own database, so size for it:

| | RAM | fits |
|---|---|---|
| t3.medium | 4 GB | too tight — Postgres alone wants 2 GB under load |
| **t3.large** | **8 GB** | **use this** |

Burstable is right here, unlike the trading box. Monitoring idles and spikes only
when someone opens a dashboard, which is exactly what CPU credits are for.

## 1. Instance

- **AMI**: Ubuntu 26.04 LTS
- **Type**: t3.large
- **Storage**: 100 GB gp3 — storage is what grows, not CPU or RAM
- **AZ**: a *different* AZ from the trading box. Monitoring that shares a failure
  domain with the thing it monitors goes dark exactly when it is needed.
- **Credit mode**: Unlimited

### Security group

| Port | Source | Why |
|---|---|---|
| 22 | your IP only | ssh |
| 80 | **your IP only** | Grafana through Traefik. See the warning below |
| 443 | your IP / VPN only | Grafana over TLS, once the overlay is in use |
| 9091 | nothing inbound | Prometheus has no auth; reach it over an ssh tunnel |
| 5433 | nothing inbound | Postgres is bound to 127.0.0.1 anyway |

Certificates use a **DNS challenge**, not an HTTP one, so port 80 never has to be
open to the internet — the security group can stay closed to everything except
you.

Prometheus needs *outbound* reach to the trading box on 9702: an egress rule
here, an ingress rule there restricted to this box's private IP.

## 2. Base packages

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 postgresql-client git
sudo usermod -aG docker $USER && newgrp docker
```

Clock — cheap, and worth doing even on a monitoring box so graph timestamps line
up with the venue's:

```bash
sudo apt-get install -y chrony
printf 'server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4\n' \
  | sudo tee /etc/chrony/conf.d/aws.conf
sudo systemctl restart chrony && chronyc tracking
```

> **The default stack serves Grafana over plain HTTP.** The admin login and
> everything the dashboards show — positions, P&L, venue balances — cross the
> network unencrypted. The port-80 rule above is the *only* thing protecting
> them, so scope it to a single address and treat widening it as a decision, not
> a convenience. Add TLS as soon as a hostname exists (step 3).

## 3. DNS and TLS — optional, and skippable at first

The stack runs with no DNS record and no certificate: Grafana is reachable by raw
IP over HTTP. That is why the base router uses a catch-all `PathPrefix(/)` rule
instead of `Host(...)` — a Host rule would not match a request addressed to an IP.

When you are ready for TLS:

1. Point an A record at the box — e.g. `carry.aegis.im` → its public IP. A
   private-only box works too; the DNS challenge never needs inbound reach.
2. Create a Cloudflare API token with **Zone:DNS:Edit** on that zone only. Not
   the Global API Key — that one can do anything to every zone on the account.
3. Set `GRAFANA_HOSTNAME`, `ACME_EMAIL` and `CF_API_TOKEN` in `.env`, then apply
   the overlay (see step 7).

## 4. Get the code onto the box

```bash
git clone git@github.com:<owner>/carry_monitor.git ~/carry_monitor
```

If the repo has no remote yet, copy it directly instead:

```bash
# from the machine holding the repo
rsync -av --exclude .venv --exclude .env --exclude letsencrypt \
  ~/carry_monitor/ ubuntu@<box>:~/carry_monitor/
```

## 5. Configure

```bash
cd ~/carry_monitor
cp .env.example .env
chmod 600 .env          # it holds an API key and two passwords
```

Fill in:

- `TS_DB_NAME` / `TS_DB_USER` / `TS_DB_PASSWORD` — invented here, not looked up.
  This database is created on first start.
- `GRAFANA_HOSTNAME` — the hostname from step 3, or **this box's own IP** while
  running without TLS. It only feeds `GF_SERVER_ROOT_URL` in HTTP mode; get it
  wrong and Grafana bounces you around on login rather than failing outright.
- `ACME_EMAIL`, `CF_API_TOKEN` — leave blank until you apply the TLS overlay
- `GF_ADMIN_PASSWORD` — it travels in the clear over HTTP, so make it unique to
  this box rather than reusing one
- `ROBOT_SERVER_IP` — the trading box (see step 6; **public IP if it is in
  another region**, since its private address is unreachable from here)
- `HL_ADDRESS` — the Hyperliquid **main account address**. Public; no key needed.
- `BINANCE_API_KEY` / `_SECRET` — **read-only**. This process never trades, so
  disable withdrawals *and* trading on the key. Do not reuse the trading key.

Leave `CARRY_INSTRUMENTS=[]`. Instruments are discovered; the seed list only
forces collection of a pair that is currently flat.

`TS_DB_HOST` / `TS_DB_PORT` / `CARRY_PROMETHEUS_URL` are set by compose — ignore
them in `.env`.

## 6. Point Prometheus at the robot

**Do not put an address in `prometheus/prometheus.yml`.** The target there is the
name `carry_tokyo`, and the name is resolved by an `extra_hosts` entry on the
prometheus service — fed from `ROBOT_SERVER_IP` in `.env`. Set that and nothing
else:

```
ROBOT_SERVER_IP=10.x.x.x
```

The name is what ends up in the `instance` label on every stored series, so it
has to stay stable. An address there means replacing the trading box splits the
history in two — old series under the old address, new ones under the new, no
panel joining across them. This is how aegis_monitor resolves `okx_colo` and
`jlp_okx_colo`, for the same reason.

Each target also carries `setup` and `host` labels. `setup` matches the `setup`
column in `carry_leg_time`, so both dashboards filter on one identifier; `host`
names the machine, kept separate because a setup can move between boxes. To add
a second book later, add a `targets:` entry with its own labels and a matching
`extra_hosts` line — no dashboard edit, since the variables read from
`label_values`.

**None of this works until the robot's metrics endpoint is reachable.** Out of
the box the Cell config uses `"Host": "localhost"`, which only accepts local
connections. On the trading box, bind it to the same name Prometheus scrapes:

```bash
# /etc/hosts on the TRADING box
10.0.10.4 carry_tokyo          # its own private address
```

```json
"Metrics": { "Host": "carry_tokyo", "Port": 9702 }
```

`Metrics.Host` is not just a bind address. `HttpListener` registers a URL prefix
and matches incoming requests on their **Host header**, so binding to the name
means the endpoint answers `carry_tokyo:9702` and returns 404 to anything that
addresses it any other way:

```
curl http://10.0.10.4:9702/metrics      -> 404
curl http://carry_tokyo:9702/metrics    -> 200
```

Prometheus sends `Host: carry_tokyo:9702` because that is the target name, so it
matches. Treat the 404 as a happy side effect, not a security control - firewall
9702 to the monitoring box regardless, since the response carries positions and
order flow.

`"+"` also works and binds every interface, answering on any Host header. On
Linux it needs no privileges; on Windows it needs a `netsh http add urlacl`
reservation.

Do not use `"0.0.0.0"`. `HttpListener` rejects it, and the exception is unhandled
— it kills the whole trading process on startup.

### Cross-region

The name is the only thing the scrape config knows, so a monitoring box in a
different region from the robot changes exactly one value:

```
ROBOT_SERVER_IP=<the robot's PUBLIC ip>
```

AWS 1:1 NATs the public address to the private one, so a listener bound to the
private IP already accepts it - no change on the robot. Open 9702 to the
monitoring box's address alone.

Be aware that `/metrics` is unauthenticated plain HTTP, so this puts positions
and order flow on the public internet with the security group as the only
control. For anything past a plumbing test, tunnel it - `autossh -L` between the
boxes, with `extra_hosts: carry_tokyo` pointed at the tunnel endpoint. The name
stays the same, so nothing else in the stack changes.

## 7. Start

```bash
docker compose up -d --build
```

With TLS, once step 3 is done:

```bash
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

The overlay swaps the catch-all router for a `Host(...)` rule, adds the ACME
resolver and the 80→443 redirect, and moves `GF_SERVER_ROOT_URL` to https. It
writes out Traefik's `command` and Grafana's `labels` in full, because Compose
**replaces** a command list rather than merging it — a partial override would
silently drop the base flags.

First start creates the databases from `sql/`. Note how those are mounted: as
**individual files**, not as `./sql:/docker-entrypoint-initdb.d`. The timescaledb
image keeps its own `000_install_timescaledb.sh` in that directory, and mounting
a directory over it shadows the extension install — `create_hypertable` then
fails with `function by_range(unknown) does not exist` and the container exits
during init.

The init scripts run **only on an empty data volume**. To re-run them:
`docker compose down -v` — which also deletes Grafana's state, since that lives
in the same Postgres.

## 8. Verify

```bash
# schema built, both databases present
docker exec carry_timescale psql -U <user> -d postgres -c '\l' | grep -E 'grafana|carry'
docker exec carry_timescale psql -U <user> -d <db> -c '\dt'
# expect carry_delta_time, carry_heartbeat, carry_instrument,
#        carry_leg_time, carry_venue_time

# monitor is scraping and not degraded
docker logs carry_monitor --tail 20
# expect: "Instruments: [...]" and "Finished."
# "Finished DEGRADED" names which sources failed

# rows landing
docker exec carry_timescale psql -U <user> -d <db> -c \
  "SELECT time, instruments, sources_failed FROM carry_heartbeat ORDER BY time DESC LIMIT 5;"

# Prometheus found the robot
curl -s localhost:9091/api/v1/targets | grep -o '"health":"[a-z]*"'

# Traefik picked up the Grafana router
docker exec carry_traefik wget -qO- http://localhost:8082/ping     # -> OK
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/login    # -> 200

# Grafana
open http://<this box's IP>

# with the TLS overlay instead:
docker logs carry_traefik 2>&1 | grep -i acme
open https://<GRAFANA_HOSTNAME>
```

Both dashboards are provisioned from disk into a **Carry** folder.

## Adding a dashboard

`grafana/provisioning/dashboards/dashboards.yml` watches `grafana/dashboards/`
and rescans every 30 seconds, so a new file appears without a restart.

**From a file.** Drop the JSON in `grafana/dashboards/`. It needs a unique `uid`,
a `title`, and to be the **raw dashboard object** — not the
`{"__inputs": [...], "dashboard": {...}}` wrapper that Grafana's "export for
sharing externally" produces, which the provisioner cannot read. Reference
datasources by their fixed uids, `carry-timescale` and `carry-prometheus`; they
are pinned in `datasources.yml` precisely so dashboard JSON needs no editing
after import.

**From the UI.** Build it, then Share → Export → Export as JSON with **"export
for sharing externally" off**. Leaving it on rewrites the datasource uids as
`${DS_...}` placeholders that provisioning cannot resolve. Save the output into
`grafana/dashboards/` and commit it.

**Exporting back into the repo.** `scripts/export-dashboards.py` writes every
dashboard in the Carry folder back into `grafana/dashboards/`:

```bash
cd ~/carry_monitor
./scripts/export-dashboards.py          # reads GF_ADMIN_PASSWORD from .env
git diff                                # review before committing
```

Standard library only, so it needs nothing installed on the box. It keeps
existing filenames by matching on `uid` rather than renaming, and strips `id` and
`version` — `id` is local to one Grafana database and collides on a rebuilt box,
and a stale `version` can stop the provisioner applying the file at all.

Read the diff rather than committing it blind: an export also captures incidental
UI state, most commonly whatever time range you happened to be looking at.

A dashboard built in the UI and never exported lives only in Postgres. That now
survives a restart, since Grafana keeps its state there — but it is not in git,
so it does not exist on a rebuilt box and `docker compose down -v` destroys it.
Worse, it is destroyed by an ordinary `git pull` too: the provisioner reapplies a
file whenever the file changes, overwriting whatever the UI had saved. Export
before you pull.

`allowUiUpdates` is true, so edits to a provisioned dashboard are saved rather
than reverted. The file wins again only when the file itself changes, so treat
the JSON as the source of truth and export UI changes back into it.

## Backup

Everything that matters — carry history *and* Grafana's own dashboards, users
and alert rules — is in one Postgres, so one dump covers both:

```bash
docker exec carry_timescale pg_dumpall -U <user> | gzip > carry-$(date +%F).sql.gz
```

This is why Grafana is configured with `GF_DATABASE_TYPE=postgres` rather than
its default SQLite: state in a container volume cannot be moved to another host,
and this is the same reason aegis_monitor does it.

## Alerts

Provisioned from `grafana/provisioning/alerting/carry-rules.yml`, in the **Carry**
folder. Every failure this book has produced was **silent**, so these watch for
the absence of health rather than for errors.

| rule | fires when | severity |
|---|---|---|
| Carry monitor is not running | no `carry_heartbeat` row for 5 min | critical |
| Carry monitor is degraded | `sources_failed` non-empty for 10 min | warning |
| Robot metrics endpoint unreachable | `up{job="carry_robot"} == 0` for 5 min | critical |
| Carry hedge is broken | `abs(carry_delta_usd) > 12` for 10 min | critical |
| Carry strategy has halted | `carry_halted == 1` for 2 min | critical |
| Clock drift against the venue | `abs(binance_timeshift) > 100 ms` for 10 min | warning |
| Engine and venue disagree | `acc_position` remote − local `> 0.5` for 5 min | critical |

### Telegram notifications

Provisioned in `carry-contactpoints.yml`. Two steps:

1. **Create a bot.** Message [@BotFather](https://t.me/BotFather), send
   `/newbot`, follow the prompts. It replies with a token like
   `8123456789:AAH...`. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Get the chat id.** Add the bot to the group (or DM it), send any message,
   then:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```
   Groups are negative (`-1001234567890`), DMs positive. Put it directly into
   `chatid:` in `carry-contactpoints.yml` — **not** into `.env`.

That split looks wrong and is deliberate. Grafana coerces interpolated
provisioning values to their natural type, so a numeric chat id arrives as a
number and validation fails with *"cannot unmarshal number into Go struct field
Config.chatid of type string"* — which stops Grafana from starting entirely.
Quoting the variable does not help; the coercion happens after expansion. The
token, being non-numeric, interpolates fine. A chat id is an identifier and
useless without the token, so it is safe to keep in a private repo.

The token goes in `settings`, not `secureSettings`, for the same
found-the-hard-way reason: validation looks for it in `settings` and otherwise
fails with *"could not find Bot Token in settings"*. Grafana encrypts it on
ingest either way — the API reads it back as `[REDACTED]`.

Restart Grafana after changing either, then **Alerting → Contact points →
carry-telegram → Test** to confirm delivery.

Running without notifications: delete `carry-contactpoints.yml`. Leaving it with
an empty token stops Grafana from starting.

Three notes on the choices, because they are not obvious:

- **"Robot unreachable" is the keystone.** Losing the scrape sends every
  Prometheus-based rule to NoData, which is not Alerting — the book would run
  unwatched while the dashboards merely looked empty. This rule is what makes the
  others trustworthy.
- **Position drift is `noDataState: OK` on purpose.** `acc_position{type="local"}`
  is created only when a fill is handled, so between a restart and the first fill
  the join returns nothing. Alerting on that would fire after every restart and
  teach everyone to ignore the rule that catches missed fills.
- **`carry_seconds_since_action` is deliberately not a rule.** It reads 0 when the
  strategy has never acted, and a healthy book at target correctly does nothing
  for hours, so it would alarm on the normal steady state.

The delta threshold of 12 matches `DeltaDiscrepancyThresholdUSD` in the Cell
config. If you change one, change the other — otherwise the alert fires at a
level the strategy is not trying to correct, or stays quiet at one it cannot.
