FROM python:3.11-slim

# System dependencies for Daphne/Channels
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Port 7860 Hugging Face ka default hai
EXPOSE 7860

# Daphne se server run karein
CMD ["daphne", "-b", "0.0.0.0", "-p", "7860", "config.asgi:application"]ss