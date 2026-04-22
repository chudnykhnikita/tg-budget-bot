FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY budget_bot.py .

RUN useradd -m -u 10001 bot && mkdir -p /data && chown bot:bot /data
USER bot

VOLUME ["/data"]

CMD ["python", "-u", "budget_bot.py"]
