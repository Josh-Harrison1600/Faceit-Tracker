FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY players.example.yaml .

ENV PYTHONUNBUFFERED=1
RUN mkdir -p /app/data
VOLUME /app/data

CMD ["python", "-m", "bot.main"]
