FROM python:3.12-slim

WORKDIR /app
COPY loader/requirements.txt loader/requirements.txt
RUN pip install --no-cache-dir -r loader/requirements.txt

COPY db ./db
COPY loader ./loader

CMD ["python", "loader/run_all.py", "--xlsx", "/app/data/Pricing_analyst_task_PY_3cases.xlsx"]
