-- Grafana keeps its own state - dashboards, users, annotations, alert rules -
-- in Postgres rather than in the container filesystem, matching aegis_monitor.
-- That is what makes the stack rebuildable and lets Grafana be moved between
-- hosts with pg_dump; a container volume would strand it on one box.
--
-- Runs first by filename order, on the database's FIRST start only.
CREATE DATABASE grafana;
