ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
ARG APT_DEBIAN_MIRROR=http://deb.debian.org/debian
ARG APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
# OpenCV/RapidOCR 的动态库必须进入运行镜像，而不只存在于开发机器。
RUN sed -i "s#http://deb.debian.org/debian-security#${APT_SECURITY_MIRROR}#g;s#http://deb.debian.org/debian#${APT_DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxcb1 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/app/.venv/bin:$PATH" \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=90 \
    UV_HTTP_RETRIES=2 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY skills ./skills
RUN python -m pip install --no-cache-dir --index-url ${PIP_INDEX_URL} uv==0.8.15 \
    && uv sync --locked --no-dev --no-editable \
    && uv cache clean
EXPOSE 8000
CMD ["uvicorn", "eic.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test-dev
RUN uv sync --locked --extra dev --no-editable \
    && uv cache clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# 默认构建仍是生产镜像；测试依赖和字体只进入 test-dev target。
FROM runtime AS production
