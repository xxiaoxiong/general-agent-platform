# Workspace Sandbox 性能基准报告

> 目的：为任务书 §17 提供**可重复的压测环境、数据和结果**，不写“性能良好”之类无数据结论。
> 数据为本机 Docker Desktop 环境实测；生产需在目标硬件复测，不构成 SLA 承诺。

## 1. 环境

| 项 | 值 |
| --- | --- |
| 执行位置 | Backend 容器（Linux 6.18 WSL2），Runner/Preview Proxy 同机 |
| 宿主 Docker | Docker Desktop，Server 28.3.2，Driver overlay2 |
| 可用 CPU | 20 vCPU |
| 可用内存 | 23.9 GiB（25055363072 bytes） |
| Sandbox 镜像 | `general-agent-platform/workspace-sandbox:1.0.0` |
| 资源档位 | standard（0.5 核请求 / 1 核上限 / 512MB 请求 / 1GB 上限 / 5GB 磁盘） |
| 平台保留 | 2 核 / 2048MB / 20GB |
| Python | 3.11.18 |
| 采样数 | 3（每轮新建 Project/Workspace/Sandbox/Service，结束删除并清理 Volume） |

## 2. 方法

脚本：`backend/scripts/sandbox_benchmark.py`

```bash
docker cp backend/scripts/sandbox_benchmark.py ai_plat_backend:/tmp/sandbox_benchmark.py
docker exec -e PYTHONPATH=/app -w /app -e BENCH_ITERATIONS=3 ai_plat_backend \
  python /tmp/sandbox_benchmark.py
```

每轮测量：

1. `provision_ready_seconds`：Project 创建 → Sandbox Provision Job → 容器启动 → workspace-agent `/health` 就绪；
2. `command_seconds`：`execute_command("echo bench-ok")` 往返；
3. `service_healthy_seconds`：`start_service(python3 -m http.server 8123)` → HTTP 健康检查通过；
4. `preview_grant_first_request_seconds`：签发 Preview Grant + 经 Preview Proxy 首次 HTTP 200；
5. Vite 启动（补充实测，见 `test_sandbox_docker_vite.py`）：`npm run dev` 发起 →
   Service `/services/vite` 变为 healthy 且首页返回 200（依赖已安装）。

失败样本不剔除，脚本输出 `failed` 计数。

## 3. 结果（2026-11-27）

| 指标 | P50 | P95 | Min | Max |
| --- | --- | --- | --- | --- |
| Sandbox Provision → Ready | 13.067s | 13.068s | 7.539s | 13.068s |
| 简单命令执行 | 0.124s | 0.150s | 0.123s | 0.150s |
| Service 启动 → Healthy | 1.678s | 1.755s | 1.613s | 1.755s |
| Preview Grant + 首次访问 | 0.040s | 0.045s | 0.039s | 0.045s |
| Vite 启动 → Healthy + 首页 200 | 2.018s | —（N=1） | 2.018s | 2.025s |

原始样本：

```json
[
  {"iteration": 1, "provision_ready_seconds": 7.539, "command_seconds": 0.15, "service_healthy_seconds": 1.755, "preview_grant_first_request_seconds": 0.045},
  {"iteration": 2, "provision_ready_seconds": 13.067, "command_seconds": 0.124, "service_healthy_seconds": 1.678, "preview_grant_first_request_seconds": 0.04},
  {"iteration": 3, "provision_ready_seconds": 13.068, "command_seconds": 0.123, "service_healthy_seconds": 1.613, "preview_grant_first_request_seconds": 0.039}
]
```

## 4. 解读与限制

- 首轮 7.5s 与后续 13.1s 的差异主要来自 Volume/Network 创建与 Docker Desktop 文件系统：首轮镜像层已缓存，但新 Volume 初始化路径不同；生产 Linux 通常更稳定。
- 采样数 3，P95 实际等于最大值；该结果用于确认量级与瓶颈，不作为统计显著性结论。
- 未测：并发创建压力（多用户）、大型 `npm install` 对生命周期操作的影响、PID/磁盘打满、Docker Daemon 重启。
- Vite 启动实测为 Runner → workspace-agent → Vite 直连（N=1，依赖已安装，2.018s）；
  未包含 `npm install` 耗时与 Preview Proxy 转发（后者实测 0.040s 量级）。
  Vite HMR 功能测试见 `docs/workspace-sandbox-delivery.md`；安装依赖受网络影响大，未纳入基准。
- 生产复测建议：目标硬件上至少 30 次采样，区分冷/热 Volume、冷/热镜像、并发用户数，并记录 Docker Storage Driver 与文件系统。
