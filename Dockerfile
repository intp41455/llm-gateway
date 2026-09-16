FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_PORT=8200

WORKDIR /srv/gateway

# 依赖先装，利用镜像层缓存
COPY pyproject.toml README.md ./
COPY app/__init__.py app/
RUN pip install --no-cache-dir -e .

# 再拷源码与配置（改代码不会让依赖层失效）
COPY app/ app/
COPY config/ config/

# 非 root 运行
RUN useradd -m -u 10001 gateway && chown -R gateway:gateway /srv/gateway
USER gateway

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"GATEWAY_PORT\",8200)}/health', timeout=4).status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${GATEWAY_PORT}"]
