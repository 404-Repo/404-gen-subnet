FROM python:3.11-slim

WORKDIR /app

RUN pip install poetry && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-dev --no-interaction

COPY . .

EXPOSE 10006

CMD ["python", "main.py"]
