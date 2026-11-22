# 巡检工单台

一个前后端分离的巡检工单项目，支持工单创建、标题搜索、状态筛选、完成与重开，以及删除操作。SQLite 文件持久化工单。

## 启动

需要 Python 3.11 或更新版本。前端使用原生 JavaScript，后端使用 Python 标准库，无需安装额外依赖。在项目根目录分别运行。

```sh
python backend/app.py
python -m http.server 8766 --bind 127.0.0.1 --directory frontend
```

访问 `http://127.0.0.1:8766`，后端默认监听 8765。可通过 `PORT` 配置后端端口，通过 `TICKET_DB` 设置数据库路径。前端可通过 `?api=http://127.0.0.1:端口` 指定 API 地址。

## 测试

保持后端运行，在同一项目目录使用 `python -m unittest discover -s tests` 验证接口。`openapi.json` 记录前后端共用的接口约定。
