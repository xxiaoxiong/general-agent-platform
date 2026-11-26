# SearXNG 集成任务书（Docker Compose 方案 A）

## 目标

将 **SearXNG 作为独立容器**集成到现有 `general-agent-platform` Docker
Compose 部署中，让后端现有 `web_search` 工具通过内网地址调用它。

采用"一个 Compose 管理多个容器"的方案：**不把 SearXNG 安装进 Backend
镜像，也不合并成单一 Docker 镜像**。不引入付费搜索 API，不重构 Agent
Runtime。

## 当前项目基础

请以仓库当前代码为准，重点检查：

-   `docker/docker-compose.yml`：现有
    MySQL、Redis、PostgreSQL、Backend、ASR、Frontend 服务编排。
-   `docker/backend.Dockerfile`：后端镜像构建。
-   `docker/.env.example`：部署环境变量示例。
-   `backend/app/core/config.py`：已存在 `SEARXNG_URL`、`SEARCH_TIMEOUT`
    配置。
-   `backend/app/services/tools/web_tools.py`：已有 SearXNG JSON API
    调用及搜索降级逻辑。

尽量复用现有实现，避免重复造轮子。

## 实施任务

### 1. 增加 SearXNG 独立服务

在 `docker/docker-compose.yml` 中新增 `searxng` 服务：

-   使用官方 `searxng/searxng`
    镜像，固定一个明确版本标签；不要生产环境直接依赖 `latest`。
-   加入现有 Compose 默认网络，供 Backend 通过服务名访问。
-   仅使用容器内部端口，不默认向宿主机或公网开放端口。
-   配置独立持久化卷（如确有缓存/数据需要）。
-   设置随机 `SEARXNG_SECRET`，通过 `docker/.env` 注入，不得硬编码在
    Compose 或配置文件中。
-   按当前 SearXNG 官方 Docker 部署方式配置必要环境变量和挂载目录。
-   是否需要 Valkey/Redis，以所选 SearXNG
    版本的官方部署要求及实际启用的配置为准；不要为了简单而遗漏运行必需依赖。

### 2. 配置 SearXNG

新增独立配置目录，例如 `docker/searxng/`：

-   基于所用版本的官方默认 `settings.yml`
    创建配置，不要只写几个字段就覆盖成不完整配置。
-   启用 JSON 输出格式，确保包含 `json`；保留需要的 HTML 格式。
-   确保服务监听容器网络接口（`0.0.0.0`），端口与 Compose 配置一致。
-   内部私有部署可按官方建议配置 limiter；不要将调试模式用于生产。
-   不要在配置中写入真实密钥。

### 3. 连接 Backend

在 Backend 的 Compose 环境变量中配置：

``` dotenv
SEARXNG_URL=http://searxng:8080
SEARCH_TIMEOUT=25
```

端口以实际 SearXNG 容器配置为准，确保服务名和端口一致。

-   Backend 使用 Docker 服务名 `searxng` 访问，不得使用 `localhost`
    或宿主机映射地址。
-   在 `depends_on` 中声明 SearXNG 启动依赖；若 SearXNG
    有可靠健康检查，再使用
    `service_healthy`，否则采用合理启动顺序并确保后端调用失败时可重试。
-   更新 `docker/.env.example`，补充 `SEARXNG_SECRET`
    等必要变量及生成方式说明。
-   检查 `config.py` 的默认值与 Compose
    注入是否一致；只有必要时才修改应用代码。

### 4. 检查现有搜索工具

检查 `backend/app/services/tools/web_tools.py` 的完整调用链：

-   保留现有 `web_search` 工具名称、参数和 Agent
    调用协议，避免影响已注册工具。
-   优先调用 SearXNG JSON
    API，确认请求参数、语言、结果数量和异常处理正确。
-   保留现有备用搜索机制，但不得把备用源伪装成 SearXNG 结果。
-   对超时、连接失败、HTTP 403（常见于 JSON
    格式未启用）、无结果等情况提供清晰日志。
-   搜索结果统一返回标题、URL、摘要、来源等已有结构；不要把大量网页正文直接塞进搜索结果。
-   本任务只要求打通搜索服务，不要求重写网页正文抓取逻辑或引入 Crawl4AI
    / Scrapling。

### 5. 文档与部署

-   更新 `docker/.env.example` 和必要的部署说明。
-   给出首次启动、查看日志、测试搜索和停止服务的命令。
-   不要提交真实 `.env`、密钥或运行数据。

## 验收标准

1.  `docker compose config` 校验通过。
2.  `docker compose up -d --build` 能启动现有服务及
    SearXNG，不破坏原有数据库卷和服务。
3.  Backend 容器内可以通过 `http://searxng:<端口>/search` 访问 SearXNG。
4.  JSON 搜索请求返回有效 JSON，而不是 403/HTML 页面。
5.  Agent 能通过原有 `web_search`
    工具完成中文和英文搜索，并返回标题、摘要及可访问 URL。
6.  SearXNG 不需要 GPU、付费 API Key 或公网暴露端口。
7.  SearXNG 单独重启不会导致 Backend
    或数据库数据丢失；服务不可用时有可读日志和合理降级。
8.  原有 Agent 工具调用、其他 Docker 服务和持久化卷不受影响。

## 交付要求

完成代码修改后，请提供：

-   修改文件清单及关键改动说明。
-   最终 SearXNG 服务配置和 Backend 连接地址。
-   实际执行过的配置校验、启动及搜索测试结果。
-   如有未能验证的上游搜索引擎或网络限制，明确说明，不要声称搜索源全部可用。

**实施原则：最小改动、复用现有 `web_search`、SearXNG 独立容器、Compose
一键部署。**
