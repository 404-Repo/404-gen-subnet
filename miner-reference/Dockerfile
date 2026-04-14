FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

WORKDIR /app

RUN pip install poetry && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock* ./
RUN poetry install --without dev --no-root --no-interaction

COPY . .

EXPOSE 10006

CMD ["python", "main.py"]
