FROM python:3.12-slim

WORKDIR /app
COPY parser_test_ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY parser_test_ui/app.py ./app.py

EXPOSE 8601
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8601"]
