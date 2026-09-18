# 羽毛球预约系统 Docker 镜像

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 容器内默认值，与 EXPOSE / HEALTHCHECK 一致；docker-compose 会在 L1 显式覆盖
    SERVER_PORT=5000 \
    # 显式声明数据目录，不依赖"BASE_DIR 恰好是 /app"这个隐含前提
    DATA_DIR=/app/data

# 安装系统依赖
# - libgl1, libglib2.0-0, libgomp1: OpenCV 运行时
# - libsm6, libxext6, libxrender1: OpenCV GUI
# - libxml2-dev, libxslt-dev: lxml
# - gcc, g++: 编译 Python C 扩展
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxml2-dev \
    libxslt-dev \
    libgomp1 \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存：src 变更不会触发依赖重装）
# 依赖清单直接从 pyproject.toml 解析——单一依赖源，不需要维护 requirements.txt
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# 再装本包（editable，依赖已满足，秒级完成）
COPY src/ /app/src/
RUN pip install --no-cache-dir --no-deps -e .

# 复制运行时文件
COPY templates/ /app/templates/
COPY static/ /app/static/

RUN mkdir -p /app/data

EXPOSE 5000

# 健康检查的唯一定义处（docker-compose 不再重复声明，避免两处参数漂移）。
# 端口从 SERVER_PORT 读，改端口不会让容器永远 unhealthy。
# start-period 15s：与原先 compose 侧的取值对齐，给冷启动留足余量。
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('SERVER_PORT','5000') + '/health', timeout=5)" || exit 1

CMD ["python", "-m", "smu_badminton.server_fastapi"]