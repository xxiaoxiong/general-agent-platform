"""自进化：Langfuse REST 读写封装（langfuse_api.py）。

运行：
    pytest backend/tests/test_evolution_langfuse_api.py -q
"""
import pytest

from app.core.config import settings
from app.services.evolution import langfuse_api


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """记录 get/post 调用的 httpx.AsyncClient 替身。"""

    gets: list = []
    posts: list = []
    get_response: _FakeResp = _FakeResp()
    post_response: _FakeResp = _FakeResp()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        _FakeClient.gets.append((url, kwargs))
        return _FakeClient.get_response

    async def post(self, url, **kwargs):
        _FakeClient.posts.append((url, kwargs))
        return _FakeClient.post_response


_ORIG_GET, _ORIG_POST = _FakeClient.get, _FakeClient.post


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(settings, "LANGFUSE_BASE_URL", "http://lf.test")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_URL", "http://lf-public.test")
    monkeypatch.setattr(settings, "LANGFUSE_PROJECT_ID", "proj-test")
    _FakeClient.gets, _FakeClient.posts = [], []
    _FakeClient.get_response = _FakeResp()
    _FakeClient.post_response = _FakeResp()
    # 还原被个别用例替换的替身方法
    _FakeClient.get, _FakeClient.post = _ORIG_GET, _ORIG_POST
    monkeypatch.setattr(langfuse_api.httpx, "AsyncClient", _FakeClient)


class TestGating:
    def test_unconfigured_degrades(self, monkeypatch):
        monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
        assert langfuse_api.is_configured() is False

    @pytest.mark.asyncio
    async def test_unconfigured_no_http(self, monkeypatch):
        monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
        assert await langfuse_api.fetch_trace_bundle("t1") is None
        await langfuse_api.write_scores("t1", [{"name": "s", "value": 1}])
        assert _FakeClient.gets == [] and _FakeClient.posts == []

    def test_trace_web_url_prefers_public(self):
        # 路由必须带 projectId 段：/project/{pid}/traces/{tid}（缺 pid 在 UI 上 404）
        assert langfuse_api.trace_web_url("tid1") == \
            "http://lf-public.test/project/proj-test/traces/tid1"

    def test_trace_web_url_falls_back_to_base(self, monkeypatch):
        monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_URL", "")
        assert langfuse_api.trace_web_url("tid1") == \
            "http://lf.test/project/proj-test/traces/tid1"

    def test_trace_web_url_requires_trace_id(self):
        assert langfuse_api.trace_web_url("") is None


class TestFetchBundle:
    @pytest.mark.asyncio
    async def test_fetch_ok(self):
        _FakeClient.get_response = _FakeResp(200, {"id": "t1"})
        _FakeClient.gets = []  # 两次 get 用同一响应即可

        async def get_dispatch(self, url, **kwargs):
            _FakeClient.gets.append((url, kwargs))
            if "/observations" in url:
                return _FakeResp(200, {"data": [{"id": "o1"}, {"id": "o2"}]})
            return _FakeResp(200, {"id": "t1"})

        _FakeClient.get = get_dispatch
        bundle = await langfuse_api.fetch_trace_bundle("t1")
        assert bundle["trace"]["id"] == "t1"
        assert len(bundle["observations"]) == 2

    @pytest.mark.asyncio
    async def test_fetch_paginates_and_caps_limit(self):
        """回归：observations limit 上限 100（更大返回 400），分页拉全。"""
        pages = {
            1: [{"id": f"o{i}"} for i in range(100)],
            2: [{"id": "o100"}],
        }

        async def get_dispatch(self, url, **kwargs):
            _FakeClient.gets.append((url, kwargs))
            if "/observations" in url:
                params = kwargs.get("params") or {}
                assert params.get("limit", 0) <= 100  # 超限 → 400 too_big
                return _FakeResp(200, {"data": pages.get(params.get("page"), [])})
            return _FakeResp(200, {"id": "t1"})

        _FakeClient.get = get_dispatch
        bundle = await langfuse_api.fetch_trace_bundle("t1")
        assert len(bundle["observations"]) == 101  # 两页拼满

    @pytest.mark.asyncio
    async def test_fetch_obs_error_logs_and_degrades(self):
        """回归：observations 查询非 200 不再静默（返回已有数据）。"""

        async def get_dispatch(self, url, **kwargs):
            _FakeClient.gets.append((url, kwargs))
            if "/observations" in url:
                return _FakeResp(400, {"error": "too_big"}, text="too_big")
            return _FakeResp(200, {"id": "t1"})

        _FakeClient.get = get_dispatch
        bundle = await langfuse_api.fetch_trace_bundle("t1")
        assert bundle["trace"]["id"] == "t1"
        assert bundle["observations"] == []

    @pytest.mark.asyncio
    async def test_fetch_not_ingested_returns_none(self):
        _FakeClient.get_response = _FakeResp(404)
        assert await langfuse_api.fetch_trace_bundle("ghost") is None

    @pytest.mark.asyncio
    async def test_fetch_http_error_degrades(self):
        async def boom(self, *args, **kwargs):
            raise RuntimeError("连接失败")

        _FakeClient.get = boom
        assert await langfuse_api.fetch_trace_bundle("t1") is None


class TestWriteScores:
    @pytest.mark.asyncio
    async def test_posts_single_objects(self):
        await langfuse_api.write_scores("t1", [
            {"name": "agent_overall", "value": 8.5, "comment": "pass"},
            {"name": "agent_efficiency", "value": 7},
        ])
        assert len(_FakeClient.posts) == 2
        url, kwargs = _FakeClient.posts[0]
        assert url == "http://lf.test/api/public/scores"
        payload = kwargs["json"]
        assert payload["traceId"] == "t1"
        assert payload["dataType"] == "NUMERIC"
        assert payload["value"] == 8.5
        assert payload["comment"] == "pass"

    @pytest.mark.asyncio
    async def test_http_error_does_not_raise(self):
        _FakeClient.post_response = _FakeResp(400, text="Bad request")
        await langfuse_api.write_scores("t1", [{"name": "s", "value": 1}])

    @pytest.mark.asyncio
    async def test_empty_scores_no_http(self):
        await langfuse_api.write_scores("t1", [])
        assert _FakeClient.posts == []
