FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor_carry.py .

# One scrape per minute, matching aegis_monitor's cadence.
RUN echo "* * * * * . /app/env.sh; python3 /app/monitor_carry.py >> /var/log/carry.log 2>&1" > /app/crontab_file \
    && crontab /app/crontab_file

COPY docker-entrypoint.sh .
RUN chmod +x /app/docker-entrypoint.sh

# Snapshots the environment for cron, then starts it. See the script for why that
# snapshot needs real shell quoting and not a sed wrapper.
CMD ["/app/docker-entrypoint.sh"]
