# 智能体对话语音听写交付

## 使用

打开平台的智能体对话页面，点击输入框旁的麦克风并授予录音权限。录音时显示计时和取消操作。再次点击原位置的停止按钮后，平台上传录音并等待识别。识别文字追加到现有草稿，可继续编辑，再通过原发送按钮或 Enter 提交给 Agent。取消录音、切换会话或离开页面会释放麦克风并丢弃未完成的听写。

生产域名使用 HTTPS。本机可通过浏览器认可的 `localhost` 或 `127.0.0.1` 使用录音。

## 启动与停止

从仓库根目录执行。平台前后端和 `asr` 服务由同一 Compose 项目管理。ASR 首次启动会从模型仓库下载 SenseVoice-Small 和 FSMN-VAD，后续复用 `general-agent-platform-asr_model_cache` 卷。

```powershell
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps asr
docker compose -f docker/docker-compose.yml logs -f asr
```

单独停用转写时执行 `docker compose -f docker/docker-compose.yml stop asr`。恢复时执行 `docker compose -f docker/docker-compose.yml up -d asr`。普通启停、重建和不带卷删除参数的 `down` 保留模型缓存。

从旧独立项目升级时，先停止旧容器。主 Compose 的 ASR 就绪后，再移除旧容器。保留 `general-agent-platform-asr_model_cache` 卷即可复用已有模型。[部署、迁移与 HTTP 验证](../deploy/asr/README.md)

### 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ASR_BASE_URL` | Docker 中为 `http://asr:8000`，本机后端为空 | 私有转写服务地址。显式留空关闭听写 |
| `ASR_MODEL` | `sensevoice` | 内部 HTTP 转写请求使用的模型别名 |
| `ASR_TIMEOUT_SECONDS` | 60 | 上游请求总时限，范围 1–120 秒 |
| `ASR_MAX_UPLOAD_MB` | 10 | 单个上传文件大小，范围 1–16 MiB |
| `ASR_MAX_AUDIO_SECONDS` | 60 | 按实际解码采样计算的录音时长，范围 1–120 秒 |

Compose 环境变量配置在 `docker/.env`。直接在宿主机启动后端时启用同样的配置需安装 FFmpeg，且后端需要访问私有 ASR 地址。

## 修改范围

| 位置 | 改动 |
| --- | --- |
| `deploy/asr/` | 以固定提交的 FunASR 官方 HTTP 示例构建 CPU 推理镜像 |
| `backend/app/api/v1/asr.py`、`backend/app/services/asr_service.py` | 提供已登录用户的能力查询和 multipart 转写。限制上传、解码、时长及上游等待，返回文本或明确的错误 |
| `backend/app/core/config.py`、`backend/app/main.py` | 注册 v1 路由并校验 ASR 配置 |
| `docker/docker-compose.yml`、`docker/backend.Dockerfile`、`frontend/nginx.conf` | 在平台 Compose 中管理 ASR、健康检查和模型卷。后端镜像包含 FFmpeg，代理设置语音接口的请求大小和时限 |
| `frontend/src/views/workspace/`、`frontend/src/api/asr.ts` | 在原 Composer 加入听写、取消、状态反馈和保留草稿的回填，复用既有 Agent 发送入口 |

浏览器把录音发送至同源 `POST /api/v1/asr/transcribe`，平台在内存中将音轨验证并归一为 16 kHz 单声道 WAV，再向 FunASR 的 `POST /v1/audio/transcriptions` 转发。ASR 临时文件位于容器内存文件系统，模型缓存是独立持久卷，ASR 容器无宿主机端口映射。后端错误日志仅记录错误类别和请求标识。支持 WebM、Ogg、分片 MP4 及 WAV 解码。[FunASR 官方 HTTP 示例](https://github.com/modelscope/FunASR/tree/d91d961e37a005837b1523bcc6b09f087877be54/examples/openai_api)

## 实测结果

| 范围 | 结果 |
| --- | --- |
| Docker Desktop CPU 服务 | 构建成功。空模型卷首次启动自动下载 SenseVoice-Small 与 FSMN-VAD。强制重建 ASR 容器后约 16 秒恢复健康，模型文件的路径、大小和修改时间一致 |
| FunASR HTTP | 官方 4.2 秒中文人声样本以 WAV、WebM/Opus、Ogg/Opus 和分片 M4A/AAC 提交，均转写为“甚至出现交易几乎停滞的情况”。静音返回空文本 |
| 浏览器端到端 | Chrome 麦克风授权、录音、停止和取消通过。真实 MediaRecorder 编码经过 Nginx、平台后端和 CPU ASR，结果追加到草稿。编辑结果后通过原 `/api/v2/runs` 发送，本地 Agent 测试模型收到编辑后的文本 |
| 异常 | 麦克风拒绝、浏览器断网、损坏音频、非法格式、超限和空识别通过。暂停 ASR 产生实际超时 504。停止 ASR 时页面提示暂不可用，手动文本发送仍成功 |
| 资源回收与隐私 | 浏览器验证停止、取消和离开页面后录音轨道结束。语音请求前后平台上传目录未出现新文件，ASR 工作完成后无录音临时文件残留，样本全文未出现在服务日志中 |
| 工程 | 前端生产构建和 17 项前端测试通过。Windows 后端全量测试 576 项通过、6 项跳过。Linux 镜像内 22 项 ASR 专项测试通过，包含四种录音格式。Nginx 配置校验通过 |

浏览器验证使用官方人声样本模拟麦克风源，浏览器原生权限、编码和上传链路均参与测试。覆盖 Chrome 桌面与 390 像素视口。Firefox 和 Safari 的相关音频格式完成了解码和实际识别验证，这两个浏览器的原生录音操作仍需各自设备上的验证。[FunASR 官方样本说明](https://raw.githubusercontent.com/modelscope/FunASR/main/examples/openai_api/README.md) · [样本标注](https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/sense_voice/README_zh.md)

已在本机将 ASR 迁入主 Compose 项目。平台前后端容器在迁移期间持续运行。新 ASR 容器挂载原模型卷，权重大小和修改时间一致。验证了平台健康接口 HTTP 200、未登录语音请求 HTTP 401，以及平台后端通过内部地址完成真实中文音频转写。
