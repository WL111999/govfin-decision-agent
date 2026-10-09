# 控制台：单页界面 + 一个薄薄的 API 层。
#
# 和向量化服务用同一个基底，理由相同：本地必然存在，不依赖 Docker Hub。
ARG BASE_IMAGE=govfin-decision-agent:1.0.0
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /console

COPY app.py /console/app.py
COPY static/ /console/static/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
