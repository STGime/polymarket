FROM python:3.11-slim

WORKDIR /app

RUN adduser --disabled-password --no-create-home botuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER botuser

ENV DRY_RUN=true
ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py", "--log-level", "INFO"]
