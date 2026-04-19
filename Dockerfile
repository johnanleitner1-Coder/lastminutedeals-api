FROM python:3.11-slim

WORKDIR /app

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser
RUN playwright install chromium --with-deps || true

# Copy source
COPY . .

CMD ["python", "tools/run_api_server.py"]
