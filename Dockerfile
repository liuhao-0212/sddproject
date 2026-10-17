# 运行镜像：每天由 cron / K8s CronJob 调用一次，生成并推送日报。
# 构建：docker build -t daily-report:$(git rev-parse --short HEAD) .
# 运行：docker run --rm --env-file .env -v "$PWD/data:/app/data" daily-report:latest
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖再拷代码：只要 lock 没变，这一层就一直命中缓存
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.20 \
    && uv sync --frozen --no-dev

COPY main.py show_report.py ./
COPY collector generator notifier shared scripts ./
COPY config.yaml.example ./

# 不以 root 运行（容器安全基线）
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# 入口是"跑一次"：容器退出即任务结束，符合定时任务型应用
ENTRYPOINT ["uv", "run", "python", "main.py"]