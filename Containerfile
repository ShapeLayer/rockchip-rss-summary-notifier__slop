FROM docker.io/library/python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY rss_notifier /app/rss_notifier
COPY configs /app/configs
ENTRYPOINT ["python", "-m", "rss_notifier"]
CMD ["--config", "/app/configs/news.toml"]
