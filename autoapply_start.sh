#!/usr/bin/env bash

set -e

echo "Waiting for IP..."

until ip addr show | grep -q '10.0.1.43'; do
	sleep 2
done

echo "IP Available"

echo "Stopping Redis..."
sudo service redis-server stop

echo "Stopping PostgreSQL..."
sudo service postgresql stop

echo "Starting AutoApply Database..."
cd /home/pi/AutoApply
docker compose up -d --wait

echo "Starting AutoApply Web..."
exec /home/pi/AutoApply/.local/bin/uv run autoapply web \
	--host 10.0.1.43 \
	--port 8000 \
	--no-open
