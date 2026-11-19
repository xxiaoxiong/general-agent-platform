# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELSCOPE_CACHE=/var/cache/funasr/modelscope \
    HF_HOME=/var/cache/funasr/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,id=agent-platform-asr-pip,target=/root/.cache/pip \
    python -m pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.8.0+cpu torchaudio==2.8.0+cpu

ARG PIP_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,id=agent-platform-asr-pip,target=/root/.cache/pip \
    python -m pip install --index-url "${PIP_INDEX_URL}" \
        funasr==1.4.14 modelscope==1.35.4 transformers==4.57.6 \
        fastapi==0.135.1 uvicorn==0.41.0 python-multipart==0.0.22 \
    && python -m pip check

WORKDIR /opt/funasr

# 固定到官方 OpenAI API 示例快速开始所用提交，并校验下载内容。
ADD --checksum=sha256:59ceb06610b41521ee708b4451a422c9ae53f378ceca6fd4da91e3637d32331c https://raw.githubusercontent.com/modelscope/FunASR/d91d961e37a005837b1523bcc6b09f087877be54/examples/openai_api/server.py /opt/funasr/server.py
ADD --checksum=sha256:82acc0a3c2be8781bcc7fccec63a3dc2df8157310a366d9ff0e725a1098f6714 https://raw.githubusercontent.com/modelscope/FunASR/d91d961e37a005837b1523bcc6b09f087877be54/examples/openai_api/smoke_test.py /opt/funasr/smoke_test.py
COPY serve.py .

RUN chmod 0444 server.py smoke_test.py \
    && useradd --create-home --uid 10001 asr \
    && install -d -o asr -g asr /var/cache/funasr

USER asr
EXPOSE 8000
CMD ["python", "serve.py", "--host", "0.0.0.0", "--port", "8000", "--device", "cpu", "--model", "sensevoice"]
