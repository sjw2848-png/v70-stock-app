import os

# V78.13.0: single source of truth for gunicorn settings (render.yaml now just points here).
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1            # in-process caches + one shared DB connection assume a single worker
threads = int(os.environ.get('GUNICORN_THREADS', '4'))
timeout = 180          # a cold full-market analysis can exceed 90s on the free plan
graceful_timeout = 30
keepalive = 5
