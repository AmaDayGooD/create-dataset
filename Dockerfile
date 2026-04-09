FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Копируем и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && playwright install-deps chromium 2>/dev/null || true

# Копируем код приложения
COPY video_screenshot.py .

# Создаём директории для данных
RUN mkdir -p /app/screenshots /app/logs /app/state

# Переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow \
    SCREENSHOT_DIR=/app/screenshots \
    STATE_DIR=/app/state

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "video_screenshot.py" || exit 1

# Запуск
CMD ["python", "video_screenshot.py"]