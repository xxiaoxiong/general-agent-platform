# SearXNG 内网搜索（Docker 集成）

`web_search` 工具的内网元搜索后端，以独立容器运行在 Compose 默认网络内，
仅供 Backend 通过 `http://searxng:8080` 调用，不映射宿主机/公网端口。

- 镜像：`searxng/searxng:2026.11.23-a7163d091`（固定版本标签，与该版本 digest 一致）
- `settings.yml`：`use_default_settings` 以镜像内置官方默认配置为基线深合并，
  仅覆盖 JSON 输出、监听地址、限流等必要字段；额外启用官方自带的
  bing/baidu/sogou/360search/quark 引擎（默认关闭）提升中英文检索可达性；
  `server.secret_key` 为占位符，运行时由环境变量 `SEARXNG_SECRET`（`docker/.env` 注入）覆盖
- `limiter.toml`：官方默认限流/反爬配置；限流状态存储复用 Compose 内现有 redis 服务
  的独立 DB(1)（`valkey://redis:6379/1`，与平台缓存 DB 0 隔离）。注意 ip_limit 对
  JSON API 请求有 4 次/小时/IP 的硬上限（公开实例防滥用），会打断 Agent 搜索，
  故内网网段经 `pass_ip` 放行，限流仅对可能接入的公网来源生效

## 常用命令

```bash
# 首次启动（在仓库根目录执行；compose 项目名 general-agent-platform）
docker compose -f docker/docker-compose.yml up -d --build

# 查看 SearXNG 日志
docker compose -f docker/docker-compose.yml logs -f searxng

# 测试搜索（JSON 输出；容器内网络访问，宿主机无端口）
docker run --rm --network general-agent-platform_default curlimages/curl:latest \
  -s "http://searxng:8080/search?q=hello&format=json" | head -c 500

# 仅重启 SearXNG（不影响 Backend 与数据库数据）
docker compose -f docker/docker-compose.yml restart searxng

# 停止服务
docker compose -f docker/docker-compose.yml down
```

## 降级行为

SearXNG 不可用时，`web_search` 会记录可读日志并按顺序降级到
DuckDuckGo / Bing / 百度外网引擎；全部失败时返回可重试的明确错误提示，
不影响其他工具与服务。

## 上游引擎说明

各上游搜索引擎的可用性取决于部署宿主机的出网环境（DNS 污染、反爬、页面结构变更等），
以实际运行日志与 `unresponsive_engines` 字段为准；SearXNG 会在 JSON 响应中如实标注
未响应引擎及原因（timeout / CAPTCHA / parsing error 等），不会把备用来源伪装成搜索结果。
