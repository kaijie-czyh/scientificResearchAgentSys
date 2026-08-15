# 科研论文 Agent 系统 — Hugging Face Spaces 部署镜像
# 基于 Python 3.10 slim（本项目要求 >= 3.10）
FROM python:3.10-slim

# 环境变量（HF Spaces 通过 Secrets 注入；端口由平台 $PORT 或 SRA_WEB_PORT 控制）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir uvicorn==0.23.2

# 拷贝项目代码（排除本地产物目录：.venv/projects/node_modules/tests 等）
COPY --exclude=.venv --exclude=projects --exclude=node_modules \
     --exclude=.git --exclude=.pytest_cache --exclude=__pycache__ \
     --exclude="*.pyc" --exclude="*.db" --exclude="*.log" \
     --exclude="*.tar.gz" . /app/

# 容器内不希望在启动时留下临时调试脚本 / 本地产物
RUN rm -rf /app/.venv 2>/dev/null; \
    mkdir -p /app/projects /app/uploads && \
    chmod -R a+rX /app

# Hugging Face Spaces 约定的健康检查端口
EXPOSE 7860

# 启动 Web 服务（同本地 `python -m web.api`，端口由 SRA_WEB_PORT 控制）
CMD ["python", "-m", "web.api"]