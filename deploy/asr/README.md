# 本地语音转写服务

语音转写是 General Agent Platform Compose 项目中的 `asr` 服务，沿用 FunASR 官方 OpenAI HTTP 示例。容器在 CPU 上加载 SenseVoice-Small 和 FSMN-VAD，模型保存在 `general-agent-platform-asr_model_cache` 卷中。录音临时文件使用容器内存文件系统，端口只在项目 Docker 网络内可达。平台后端通过 `http://asr:8000` 调用，浏览器使用平台的同源语音接口。

## 启停与检查

在仓库根目录执行。首次启动需要访问模型仓库，下载时间取决于网络。

```powershell
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps asr
docker compose -f docker/docker-compose.yml logs -f asr
```

状态变为 `healthy` 后，可以检查已加载模型并使用官方中文录音验证转写。

```powershell
docker compose -f docker/docker-compose.yml exec asr python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
docker compose -f docker/docker-compose.yml exec asr python smoke_test.py /tmp/funasr-sample.wav --response-format json
```

`/health` 应显示 `device: cpu` 和 `models_loaded: ["sensevoice"]`。转写端点为 `POST /v1/audio/transcriptions`，参数为 multipart `file`、`model=sensevoice` 和 `response_format=json`，成功响应包含 `text`。

单独停止或启动语音转写时，平台其余服务可继续运行。

```powershell
docker compose -f docker/docker-compose.yml stop asr
docker compose -f docker/docker-compose.yml up -d asr
```

停止、重建和普通 `down` 保留模型卷。已有缓存时服务复用模型文件，ModelScope 启动时可能访问仓库查询元数据。

## 从旧独立部署迁移

如果曾使用默认的独立项目 `general-agent-platform-asr`，先停止旧容器，再用平台 Compose 启动 ASR。新容器就绪后移除旧容器，避免同一 Docker 网络上出现两个 `asr` 地址。

```powershell
docker stop general-agent-platform-asr-asr-1
docker compose -f docker/docker-compose.yml up -d --no-build --wait --wait-timeout 120 asr
```

新容器通过健康检查后，清理旧容器。

```powershell
docker rm general-agent-platform-asr-asr-1
```

如需回退，先用主 Compose 停止 `asr`，再用 `docker start general-agent-platform-asr-asr-1` 恢复旧容器。迁移时请保留 `general-agent-platform-asr_model_cache` 卷。

上游示例和冒烟脚本固定到 FunASR 的快速开始提交。服务只注册本部署的 SenseVoice 模型，保持 HTTP 示例的请求格式。详见 [FunASR 官方 HTTP 示例](https://github.com/modelscope/FunASR/tree/d91d961e37a005837b1523bcc6b09f087877be54/examples/openai_api) 和 [SenseVoiceSmall 模型](https://modelscope.cn/models/iic/SenseVoiceSmall)。
