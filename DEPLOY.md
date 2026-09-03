# Deploying on Ubuntu 26.04 / t3.medium

t3.medium is 4 GB. That is enough for **Prometheus + Grafana + the cron monitor**
pointed at an existing TimescaleDB. It is not comfortable with the bundled
database as well — Postgres, Prometheus and Grafana together want 5–6 GB, so use
`t3.large` if this box must own its own database.

Burstable is the right family here, unlike the trading box. Monitoring is idle
with occasional query spikes when someone opens a dashboard, which is exactly
what CPU credits are for.

## 1. Instance

- **AMI**: Ubuntu 26.04 LTS
- **Type**: t3.medium (external DB) or t3.large (bundled DB)
- **Storage**: 100 GB gp3 — storage is the thing that grows, not CPU or RAM
- **AZ**: a *different* AZ from the trading box. Monitoring that shares a failure
  domain with the thing it monitors goes dark exactly when it is needed.
- **Credit mode**: Unlimited

### Security group

| Port | Source | Why |
|---|---|---|
| 22 | your IP only | ssh |
| 3001 | your IP / VPN only | Grafana. **Never 0.0.0.0/0** — it holds positions and P&L |
| 9091 | nothing inbound | Prometheus; reach it over an ssh tunnel |

Prometheus needs *outbound* reach to the trading box on 9702. That is an egress
rule on this box and an ingress rule on the trading box, restricted to this
box's private IP.

## 2. Base packages

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 postgresql-client git
sudo usermod -aG docker $USER && newgrp docker
```

Clock — cheap and worth doing even on a monitoring box, so graph timestamps line
up with the venue's:

```bash
sudo apt-get install -y chrony
printf 'server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4\n' \
  | sudo tee /etc/chrony/conf.d/aws.conf
sudo systemctl restart chrony && chronyc tracking
```

## 3. Get the code onto the box

This repo has **no remote yet**. Either push it somewhere first, or copy it
directly:

```bash
# from the machine holding the repo
rsync -av --exclude .venv --exclude .env ~/carry_monitor/ ubuntu@<box>:~/carry_monitor/
```

## 4. Configure

```bash
cd ~/carry_monitor
cp .env.example .env
chmod 600 .env          # it holds an API key
```

Fill in:

- `TS_DB_*` — point at your existing TimescaleDB, or at `carry_timescale` if
  using the bundled one
- `HL_ADDRESS` — the Hyperliquid **main account address**. Public; no key needed.
- `BINANCE_API_KEY` / `_SECRET` — **read-only**. This process never trades, so
  disable withdrawals *and* trading on the key. Do not reuse the trading key.
- `GF_ADMIN_PASSWORD`
- `CARRY_PROMETHEUS_URL=http://carry_prometheus:9090`

Leave `CARRY_INSTRUMENTS=[]`. Instruments are discovered; the seed list only
forces collection of a pair that is currently flat.

## 5. Schema

```bash
psql -h <ts-host> -U <user> -d <db> -f sql/001_carry.sql
psql -h <ts-host> -U <user> -d <db> -f sql/002_registry.sql
```

With the bundled database these run automatically on first start, since `sql/` is
mounted into the init directory.

## 6. Point Prometheus at the robot

Edit `prometheus/prometheus.yml` and replace `host.docker.internal:9702` with the
trading box's private IP.

**This will not work until the robot's metrics endpoint is reachable.** The Cell
config currently uses `"Host": "localhost"`, which only accepts local
connections. On the trading box set:

```json
"Metrics": { "Host": "+", "Port": 9702 }
```

`+` binds all interfaces. On Linux that needs no extra privileges; on Windows it
needs a `netsh http add urlacl` reservation. Then firewall 9702 to this box only
— the endpoint exposes positions and order flow.

Do not use `"0.0.0.0"`. `HttpListener` rejects it, and the exception is unhandled
— it kills the whole trading process on startup.

## 7. Start

```bash
docker compose up -d                          # external DB
docker compose --profile bundled-db up -d     # bundled DB (t3.large)
```

## 8. Verify

```bash
# monitor is scraping and not degraded
docker logs carry_monitor --tail 20
# expect: "Instruments: [...]" and "Finished."
# "Finished DEGRADED" names which sources failed

# rows landing
psql -h <ts-host> -U <user> -d <db> -c \
  "SELECT time, instruments, sources_failed FROM carry_heartbeat ORDER BY time DESC LIMIT 5;"

# Prometheus found the robot
curl -s localhost:9091/api/v1/targets | grep -o '"health":"[a-z]*"'

# Grafana
ssh -L 3001:localhost:3001 ubuntu@<box>    # then open http://localhost:3001
```

Both dashboards are provisioned from disk into a "Carry" folder. They are
read-only in the sense that editing them in the UI will be overwritten on
restart — change the JSON in git instead.

## What to alert on

Every failure this book has produced was **silent**, so absence of errors is not
health. The four signals worth wiring, roughly in order of value:

| signal | query | means |
|---|---|---|
| monitor dead | `max(carry_heartbeat.time)` older than 5 min | no monitoring at all |
| degraded scrape | `sources_failed <> ''` | collecting on stale or partial data |
| strategy stuck | `carry_seconds_since_action` growing | process alive but doing nothing |
| position drift | `acc_position{type="remote"} - acc_position{type="local"}` | engine and venue disagree — fills missed |
| clock drift | `binance_timeshift` beyond ±100 ms | leading indicator for `-1021` order rejections |

The last two are the ones that would have caught the incidents that actually cost
money, and both were already being measured before anyone looked at them.
