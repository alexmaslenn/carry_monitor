#!/bin/sh
set -e

# cron does not inherit the container environment, so snapshot it into a file the
# job sources. The quoting here is the whole point of this script.
#
# The obvious version - printenv piped through sed, wrapping each value in double
# quotes - is wrong for any value that contains a quote or a space. CARRY_TARGETS
# is JSON and contains both:
#
#   export CARRY_TARGETS="{"DOGE": -20}"    ->    CARRY_TARGETS={DOGE:
#
# The shell strips the inner quotes and splits on the space, and the job then
# dies in json.loads naming the variable but not the reason. shlex.quote produces
# a single-quoted, fully escaped literal that survives sourcing unchanged.
python3 - <<'PY'
import os, re, shlex

with open("/app/env.sh", "w") as f:
    for key, value in os.environ.items():
        # Skip anything that is not a valid shell identifier; docker can inject
        # names that would make the sourced file a syntax error.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            f.write("export %s=%s\n" % (key, shlex.quote(value)))
PY

# It holds the Binance secret and the database password.
chmod 600 /app/env.sh

touch /var/log/carry.log
cron

# Surfaces the cron job's stdout and stderr as container logs, which is the only
# place a failed scrape is visible.
exec tail -f /var/log/carry.log
