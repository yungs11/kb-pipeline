FROM python:3.12-slim

# curl — compose healthcheck 가 쓴다(docker-compose.airgap.yml). 없으면 앱은 정상
# 서빙해도 healthcheck 가 항상 "curl: executable file not found"로 실패한다.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY parser_test_ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY parser_test_ui/app.py ./app.py

EXPOSE 8601
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8601"]
