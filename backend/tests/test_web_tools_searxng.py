"""web_search 工具的 SearXNG 调用链测试（Docker 集成任务书 + 网络检索集成）。

覆盖：JSON 结果解析、HTTP 403（JSON 格式未启用）可读报错、
内网直连不经出口代理、失败后按序降级到备用引擎、
结构化来源 metadata.sources、URL 去重、查询语言路由。
"""
import pytest
import httpx

from app.core.config import settings
from app.services.tools import web_tools
from app.services.tools.result import ToolResult


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", invalid_json=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("Expecting value: line 1 column 1")
        return self._json_data


def _patch_client(monkeypatch, responses, calls):
    """把 httpx.AsyncClient 替换为按 URL 路由应答的假客户端。

    responses: {url 关键词: _FakeResponse | Exception}；未命中默认连接失败。
    """
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.init_kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            calls.append({"method": "GET", "url": url, "kwargs": kwargs, "init_kwargs": self.init_kwargs})
            return self._respond(url)

        async def post(self, url, **kwargs):
            calls.append({"method": "POST", "url": url, "kwargs": kwargs, "init_kwargs": self.init_kwargs})
            return self._respond(url)

        @staticmethod
        def _respond(url):
            for key, value in responses.items():
                if key in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_searxng_json_results_sources_and_direct_connection(monkeypatch):
    """SearXNG 返回 JSON 结果时优先采用，携带结构化来源，且内网调用不走出口代理。"""
    calls = []
    _patch_client(
        monkeypatch,
        {
            "searxng": _FakeResponse(
                200,
                json_data={
                    "results": [
                        {"title": "测试标题", "url": "https://example.com/a", "content": "测试摘要"},
                        {"title": "第二条", "url": "https://example.com/b", "content": ""},
                    ]
                },
            ),
        },
        calls,
    )
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "http://proxy:8080")

    ret = await web_tools.execute_web_search({"query": "测试", "max_results": 5})

    assert isinstance(ret, ToolResult)
    assert ret.status == "succeeded"
    text = ret.text
    assert "SearXNG" in text
    assert "测试标题" in text and "https://example.com/a" in text

    # 结构化来源（metadata 旁路通道）：id/title/url/desc
    sources = ret.metadata.get("sources") or []
    assert [s["id"] for s in sources] == [1, 2]
    assert sources[0]["title"] == "测试标题"
    assert sources[0]["url"] == "https://example.com/a"
    assert sources[0]["desc"] == "测试摘要"
    assert ret.metadata.get("engine") == "SearXNG"

    searxng_calls = [c for c in calls if "searxng" in c["url"]]
    assert searxng_calls, "应当调用 SearXNG"
    params = searxng_calls[0]["kwargs"]["params"]
    assert params["format"] == "json"
    # 中文查询路由 zh-CN
    assert params["language"] == "zh-CN"
    # 内网调用必须直连：不得携带出口代理配置
    assert not searxng_calls[0]["init_kwargs"].get("proxy")


@pytest.mark.asyncio
async def test_searxng_published_date_preserved(monkeypatch):
    """SearXNG 的 publishedDate 必须保留到来源与文本输出（时效判断依据）。"""
    calls = []
    _patch_client(
        monkeypatch,
        {
            "searxng": _FakeResponse(
                200,
                json_data={
                    "results": [
                        {"title": "新文章", "url": "https://example.com/new", "content": "摘要",
                         "publishedDate": "2026-11-20T08:00:00Z"},
                        {"title": "无日期", "url": "https://example.com/old", "content": ""},
                    ]
                },
            ),
        },
        calls,
    )
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")

    ret = await web_tools.execute_web_search({"query": "测试", "max_results": 5})

    assert ret.status == "succeeded"
    sources = ret.metadata.get("sources") or []
    assert sources[0]["published"] == "2026-11-20T08:00:00Z"
    assert sources[1]["published"] == ""
    # 文本输出也带发布时间（模型据此判断时效）
    assert "2026-11-20" in ret.text


@pytest.mark.asyncio
async def test_searxng_http_403_reports_json_format_hint(monkeypatch, caplog):
    """HTTP 403（JSON 格式未启用）需要给出清晰报错与日志。"""
    calls = []
    _patch_client(monkeypatch, {"searxng": _FakeResponse(403)}, calls)
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "")

    with caplog.at_level("WARNING", logger="app.services.tools.web_tools"):
        ret = await web_tools.execute_web_search({"query": "anything"})

    # 引擎有响应（403）说明网络可达：属于配置/确定性失败，不自动重试，但要给出 JSON 格式提示
    assert isinstance(ret, ToolResult)
    assert ret.status == "failed" and not ret.retryable
    assert "HTTP 403" in str(ret.error)
    assert "JSON 格式未启用" in str(ret.error)
    assert any("403" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_searxng_down_falls_back_to_backup_engine(monkeypatch):
    """SearXNG 不可用时按序降级到备用引擎，不伪装成 SearXNG 结果。"""
    calls = []
    bing_html = (
        '<li class="b_algo"><h2><a href="https://example.com/fallback">备用结果</a></h2>'
        "<p>备用摘要</p></li>"
    )
    _patch_client(
        monkeypatch,
        {
            "searxng": httpx.ConnectError("connection refused"),
            "bing.com": _FakeResponse(200, text=bing_html),
        },
        calls,
    )
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "")

    ret = await web_tools.execute_web_search({"query": "fallback"})

    assert isinstance(ret, ToolResult)
    assert ret.status == "succeeded"
    text = ret.text
    assert "Bing" in text
    assert "备用结果" in text
    assert "SearXNG" not in text.split("\n")[0]  # 来源标注不得伪装成 SearXNG
    # 降级结果同样携带结构化来源
    assert [s["url"] for s in ret.metadata["sources"]] == ["https://example.com/fallback"]


@pytest.mark.asyncio
async def test_search_results_dedup_by_normalized_url(monkeypatch):
    """同一页面的斜杠/大小写变体应去重，来源 id 连续。"""
    calls = []
    _patch_client(
        monkeypatch,
        {
            "searxng": _FakeResponse(
                200,
                json_data={
                    "results": [
                        {"title": "A", "url": "https://Example.com/page/", "content": ""},
                        {"title": "A-dup", "url": "https://example.com/page", "content": ""},
                        {"title": "B", "url": "https://example.com/other", "content": ""},
                    ]
                },
            ),
        },
        calls,
    )
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "")

    ret = await web_tools.execute_web_search({"query": "测试", "max_results": 5})

    sources = ret.metadata["sources"]
    assert [s["id"] for s in sources] == [1, 2]
    assert sources[0]["title"] == "A"  # 保留首个出现


@pytest.mark.asyncio
async def test_zero_results_from_reachable_engine_is_not_retryable_success(monkeypatch):
    """引擎可达但 0 结果：不得伪装成功（会污染检索证据），也不应自动重试同一查询。"""
    calls = []
    _patch_client(monkeypatch, {"searxng": _FakeResponse(200, json_data={"results": []})}, calls)
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "")

    ret = await web_tools.execute_web_search({"query": "site:example.com 无结果查询"})

    assert isinstance(ret, ToolResult)
    assert ret.status == "failed"
    assert ret.retryable is False
    assert "未找到可用结果" in str(ret.error)
    assert "SearXNG: 返回 0 条结果" in str(ret.error)
    # 引擎可达时不得误报“无法连接到搜索引擎”，也不能引导模型凭记忆编造
    assert "无法连接到搜索引擎" not in str(ret.error)
    assert "使用你的知识" not in str(ret.error)
    assert "请调整查询词" in str(ret.error)


@pytest.mark.asyncio
async def test_all_engines_unreachable_is_retryable_network_failure(monkeypatch):
    """所有引擎都连不上时才是网络类瞬态失败，允许有限重试。"""
    calls = []
    _patch_client(
        monkeypatch,
        {
            "searxng": httpx.ConnectError("connection refused"),
            "duckduckgo": httpx.ConnectError("unreachable"),
            "bing.com": httpx.ConnectError("unreachable"),
            "baidu.com": httpx.ConnectError("unreachable"),
        },
        calls,
    )
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(settings, "AGENT_EGRESS_PROXY", "")

    ret = await web_tools.execute_web_search({"query": "任意查询"})

    assert isinstance(ret, ToolResult)
    assert ret.status == "failed" and ret.retryable is True
    assert "无法连接到搜索引擎" in str(ret.error)
    assert "不要凭记忆编造来源" in str(ret.error)


@pytest.mark.asyncio
async def test_fetch_webpage_without_readable_body_is_failed(monkeypatch):
    """空正文/纯脚本页面不是可用来源：按失败上报，不伪装成抓取成功。"""
    calls = []
    _patch_client(
        monkeypatch,
        {"example.com": _FakeResponse(200, text="<html><head><title>空页</title></head>"
                                               "<body><script>var x=1;</script></body></html>")},
        calls,
    )

    ret = await web_tools.execute_fetch_webpage({"url": "https://example.com/empty"})

    assert isinstance(ret, ToolResult)
    assert ret.status == "failed"
    assert ret.retryable is False
    assert "未提取到可用正文" in str(ret.error)


def test_bing_parser_handles_attributes_and_missing_closing_li():
    """Bing 的 b_algo 条目带附加属性且常省略 </li>，备用引擎不能因此解析为 0 条。"""
    html = (
        '<ol id="b_results"><li class="b_algo" data-id iid=SERP.1>'
        '<h2><a href="https://example.com/a">标题 A &amp; B</a></h2><p>描述 A</p>'
        '<li class="b_algo" data-id iid=SERP.2>'
        '<h2><a href="https://example.com/b">标题 B</a></h2><p>描述 B</p></ol>'
    )
    rows = web_tools._parse_bing_html(html, 5)
    assert [row["url"] for row in rows] == ["https://example.com/a", "https://example.com/b"]
    assert rows[0]["title"] == "标题 A & B"


def test_query_language_routing():
    assert web_tools._query_language("Python 编程") == "zh-CN"
    assert web_tools._query_language("large language model") == "en"
