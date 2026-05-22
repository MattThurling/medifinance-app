# syntax=docker/dockerfile:1

# ---- Stage 1: build Tailwind/DaisyUI CSS ----
FROM node:22-slim AS css
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
# Copy the source the Tailwind build scans (input.css + templates via @source)
COPY . .
RUN npm run build:css


# ---- Stage 2: Python runtime ----
FROM python:3.13-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bring in the built CSS and the Lucide icon set. The {% icon %} tag reads SVGs
# from node_modules/lucide-static at render time, so it must exist at runtime.
COPY --from=css /app/static/dist ./static/dist
COPY --from=css /app/node_modules/lucide-static ./node_modules/lucide-static

# Collect static with production storage (hashed + compressed). DEBUG=0 selects
# the WhiteNoise manifest storage; no DB or secrets are needed for this step.
# Ignore the Tailwind *source* dir — only the built dist/output.css is served.
RUN DJANGO_DEBUG=0 python manage.py collectstatic --noinput --ignore=src

EXPOSE 8080

# Cloud Run sets $PORT. One worker + threads is the recommended shape since
# Cloud Run scales horizontally; --timeout 0 lets Cloud Run own the request timeout.
CMD exec gunicorn medifinance.wsgi:application --bind :$PORT --workers 1 --threads 8 --timeout 0
