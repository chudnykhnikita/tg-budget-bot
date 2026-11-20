FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY budget_bot.py .
COPY proxy_failover.py .
COPY budget_map.py .
COPY sheets_budget.py .

RUN mkdir -p /data

VOLUME ["/data"]

CMD ["python", "-u", "budget_bot.py"]
