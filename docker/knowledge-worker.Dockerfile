# Knowledge Ingestion Worker 镜像：在 Backend 依赖之上安装 Docling / OCR / LibreOffice。
# 独立进程运行（python -m app.knowledge_worker），负责解析、OCR、切分、Embedding 与
# Qdrant 写入，不在 FastAPI 请求进程中执行 CPU 密集型任务。
# 构建上下文为仓库根目录（compose: context: ..）。
FROM python:3.11-slim

WORKDIR /app

# libgl1 / libglib2.0-0：RapidOCR 依赖的 OpenCV 运行库；
# libreoffice-writer/calc/impress：旧版 doc/xls/ppt 转换。
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    libgl1 \
    libglib2.0-0 \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Docling 依赖 torch：默认 PyPI Linux 轮子会拉取 GB 级 CUDA 依赖（cudnn/cublas 等）。
# 预装 CPU-only torch / torchvision；后续 requirements 检测到已满足版本即复用。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --index-url ${TORCH_INDEX_URL} torch torchvision

COPY backend/requirements.txt backend/requirements-worker.txt ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements-worker.txt

# 预下载 Embedding 与 Docling 版面/表格模型（运行时不再依赖外网；离线部署必需）
ARG HF_ENDPOINT=https://huggingface.co
ENV HF_ENDPOINT=${HF_ENDPOINT}
ENV HF_HOME=/opt/hf
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
# Docling 预下载模型目录（离线解析必须指向该目录，否则会尝试联网下载）
ENV DOCLING_ARTIFACTS_PATH=/root/.cache/docling/models
RUN mkdir -p /opt/fastembed /opt/hf /app/data/knowledge \
    && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-zh-v1.5'); print('embedding model cached')" \
    && docling-tools models download layout tableformer tableformerv2 code_formula rapidocr

COPY backend/ /app/

CMD ["python", "-m", "app.knowledge_worker"]
