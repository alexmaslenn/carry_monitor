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

# cron does not inherit the container environment, so snapshot it at start.
CMD printenv | sed 's/^\([^=]*\)=\(.*\)$/export \1="\2"/' > /app/env.sh \
    && touch /var/log/carry.log \
    && cron && tail -f /var/log/carry.log
