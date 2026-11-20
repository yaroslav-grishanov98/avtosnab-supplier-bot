#!/bin/sh
set -e

if [ "$ROLE" = "worker" ]; then
  echo "[entrypoint] Starting Celery worker..."
  exec celery -A config worker -Q "${AI_CLEANUP_QUEUE:-ai_normalization}" --prefetch-multiplier=1 -l info
elif [ "$ROLE" = "notifications-worker" ]; then
  echo "[entrypoint] Starting Celery notifications worker..."
  exec celery -A config worker \
    -Q "${NOTIFICATION_QUEUE:-notifications}" \
    --concurrency="${NOTIFICATION_WORKER_CONCURRENCY:-2}" \
    --prefetch-multiplier=1 \
    -l info
elif [ "$ROLE" = "iiko-worker" ]; then
  exec celery -A config worker -n 'iiko-sync@%h' \
    -Q "${ACCOUNTING_QUEUE_SYNC:-iiko_sync}" --pool=threads \
    --concurrency="${ACCOUNTING_WORKER_CONCURRENCY:-12}" --prefetch-multiplier=1 -l info
elif [ "$ROLE" = "iiko-manual-worker" ]; then
  exec celery -A config worker -n 'iiko-manual@%h' \
    -Q "${ACCOUNTING_QUEUE_MANUAL:-iiko_manual},${ACCOUNTING_QUEUE_CONTROL:-iiko_control}" --pool=threads \
    --concurrency=3 --prefetch-multiplier=1 -l info
elif [ "$ROLE" = "beat" ]; then
  echo "[entrypoint] Starting Celery Beat..."
  exec celery -A config beat -l info
elif [ "$ROLE" = "migrate" ]; then
  # 2026-08-12: found while chasing a deploy failure — this script never read
  # $@/CMD at all, so docker-compose.yml's `migrate` service `command:
  # python manage.py migrate ...` override was silently a no-op. That
  # container fell into the same "else" branch as `app` below and ran the
  # full web flow (migrate + collectstatic + gunicorn, forever) instead of
  # running migrate once and exiting. So `migrate` was never actually a
  # separate step — it was a second, unpublished `app` instance, and BOTH
  # containers independently ran `manage.py migrate` concurrently on every
  # deploy with no coordination between them. Usually resolved within a
  # couple of silent retries; heavier data migrations (hundreds of
  # category-tree rows) widened that race past the healthcheck budget and
  # started failing real deploys. See
  # docs/wiki/logs/2026-08-12-deploy-migrate-race-root-cause.md.
  #
  # Explicit ROLE=migrate (set by docker-compose.yml) makes this container
  # actually just run migrate and exit — `app` now waits for it via
  # `depends_on: migrate: condition: service_completed_successfully`,
  # so there is exactly one process ever running migrations per deploy.
  echo "[entrypoint] Running migrations..."
  exec python manage.py migrate --noinput --settings=config.settings
else
  echo "[entrypoint] Collecting static files..."
  python manage.py collectstatic --noinput --settings=config.settings

  echo "[entrypoint] Starting Gunicorn..."
  exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
fi
