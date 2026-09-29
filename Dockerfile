# 클라우드 서버용 이미지 (Render 등). PC 에서는 start.bat / start.sh 를 쓰세요.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    JISIKIN_DATA_DIR=/app/data \
    JISIKIN_HOST=0.0.0.0 \
    JISIKIN_SECURE_COOKIE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jisikin ./jisikin
COPY config.example.yaml .env.example ./

# 설정·API 키·DB 는 /app/data (영구 디스크)에 저장된다
RUN mkdir -p /app/data
EXPOSE 10000
CMD ["python", "-m", "jisikin", "serve", "--no-browser"]
