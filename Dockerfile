FROM gh-proxy.org/docker/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# 🔥 必须在这里先创建目录（修复挂载错误）
RUN mkdir -p /app/data /app/static/music

# 腾讯云pip源
RUN pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple && \
    pip config set install.trusted-host mirrors.cloud.tencent.com

RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]