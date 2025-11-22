FROM python:3.10-slim

# FFmpeg o'rnatish (Konvertatsiya uchun shart)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fayllar tushadigan papka
RUN mkdir -p converts

CMD ["python", "converter_bot.py"]