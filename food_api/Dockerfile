FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Все модули приложения, а не только api.py: он импортирует nutrition и
# recognize на уровне модуля, а те читают recognize_prompt.md и overrides.json.
COPY api.py nutrition.py recognize.py recognize_prompt.md overrides.json ./

RUN mkdir -p /app/data

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
