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
| 443 | your IP / VPN only | Grafana through Traefik. **Never 0.0.0.0/0** — it shows positions and P&L |
| 80 | your IP / VPN only | redirect to 443 only |
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

## 3. DNS

Traefik needs a hostname and a Cloudflare token before it can issue a
certificate:

1. Point an A record at the box — e.g. `carry.aegis.im` → its public IP. A
   private-only box works too; the DNS challenge never needs inbound reach.
2. Create a Cloudflare API token with **Zone:DNS:Edit** on that zone only. Not
   the Global API Key — that one can do anything to every zone on the account.

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
- `GRAFANA_HOSTNAME` — the name from step 3
- `ACME_EMAIL`, `CF_API_TOKEN` — for the certificate
- `GF_ADMIN_PASSWORD`
- `ROBOT_SERVER_IP` — private IP of the trading box (see step 6)
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

# certificate issued
docker logs carry_traefik 2>&1 | grep -i acme

# Grafana
open https://<GRAFANA_HOSTNAME>
```

Both dashboards are provisioned from disk into a "Carry" folder. Editing them in
the UI is overwritten on restart — change the JSON in git instead.

## Backup

Everything that matters — carry history *and* Grafana's own dashboards, users
and alert rules — is in one Postgres, so one dump covers both:

```bash
docker exec carry_timescale pg_dumpall -U <user> | gzip > carry-$(date +%F).sql.gz
```

This is why Grafana is configured with `GF_DATABASE_TYPE=postgres` rather than
its default SQLite: state in a container volume cannot be moved to another host,
and this is the same reason aegis_monitor does it.

## What to alert on

Every failure this book has produced was **silent**, so absence of errors is not
health. The signals worth wiring, roughly in order of value:

| signal | query | means |
|---|---|---|
| monitor dead | `max(carry_heartbeat.time)` older than 5 min | no monitoring at all |
| degraded scrape | `sources_failed <> ''` | collecting on stale or partial data |
| strategy stuck | `carry_seconds_since_action` growing | process alive but doing nothing |
| position drift | `acc_position{type="remote"} - acc_position{type="local"}` | engine and venue disagree — fills missed |
| clock drift | `binance_timeshift` beyond ±100 ms | leading indicator for `-1021` order rejections |

The last two would have caught the incidents that actually cost money, and both
were already being measured before anyone looked at them.
