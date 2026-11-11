FROM python:3.12-slim
WORKDIR /app
COPY alert_bot.py ./
# watches.json, .env and state.json are mounted (see docker-compose.yml)
ENV PYTHONUNBUFFERED=1
CMD ["python", "alert_bot.py", "--watches", "/data/watches.json", "--state", "/data/state.json"]
