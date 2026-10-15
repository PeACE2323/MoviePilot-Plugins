"""MediaAutomatorSync 纯逻辑测试。

宿主依赖（app.*）在测试环境不可用，因此这里用轻量桩替换 MoviePilot SDK，
只验证插件自身的日志解析、事件分类、媒体提取与游标读取逻辑。
测试通过 ``app.plugins.<plugin_id>`` 路径导入源码，与生产加载方式一致。
"""

import importlib.util
import json
import sys
import types
from enum import Enum as _Enum
from pathlib import Path

try:  # pytest 可选：run_tests.py 在无 pytest 环境下直接调用测试函数
    import pytest
except ModuleNotFoundError:  # pragma: no cover - 仅在无 pytest 环境触发
    class _PytestShim:
        """提供最小 fixture 装饰器，使测试模块在无 pytest 时也能导入。"""

        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(func):
                return func

            # 兼容 @pytest.fixture 与 @pytest.fixture() 两种写法
            return decorator(args[0]) if args and callable(args[0]) else decorator

    pytest = _PytestShim()

# 测试位于 <repo>/tests/v3/<plugin_id>/，插件源码位于 <repo>/plugins.v3/<plugin_id>/。
PLUGIN_INIT = Path(__file__).resolve().parents[3] / "plugins.v3" / "mediaautomatorsync" / "__init__.py"


def _install_stubs() -> None:
    """安装 MoviePilot SDK 的最小替身，使插件模块可以导入。"""
    if "app" in sys.modules:
        return

    app = types.ModuleType("app")

    # app.sdk.logging
    sdk = types.ModuleType("app.sdk")
    logging_mod = types.ModuleType("app.sdk.logging")

    class _Logger:
        """收集日志调用，便于断言。"""

        def __init__(self) -> None:
            self.records = []

        def _log(self, level, message, **kwargs):
            self.records.append((level, message))

        def info(self, message, **kwargs):
            self._log("info", message, **kwargs)

        def warning(self, message, **kwargs):
            self._log("warning", message, **kwargs)

        def error(self, message, **kwargs):
            self._log("error", message, **kwargs)

        def debug(self, message, **kwargs):
            self._log("debug", message, **kwargs)

    logging_mod.logger = _Logger()

    # app.sdk.plugin._PluginBase
    plugin_mod = types.ModuleType("app.sdk.plugin")

    class _PluginBase:
        """插件基类替身，只保留插件用到的存储与配置接口。"""

        plugin_name = ""
        plugin_desc = ""
        plugin_order = 9999

        def __init__(self) -> None:
            self._store = {}
            self._config = {}

        def get_data(self, key=None, plugin_id=None):
            return self._store.get(key)

        def save_data(self, key, value, plugin_id=None):
            self._store[key] = value

        def update_config(self, config, plugin_id=None):
            self._config = config
            return True

        def get_data_path(self, plugin_id=None):
            return Path(".")

        def post_message(self, **kwargs):
            return None

    plugin_mod._PluginBase = _PluginBase

    # app.sdk.network.AsyncRequestUtils：可编程的假 HTTP 客户端。
    network_mod = types.ModuleType("app.sdk.network")

    class FakeHeaders:
        """最小响应头对象，模拟 httpx2 的 Headers（支持 get_list）。"""

        def __init__(self, pairs=None):
            self._pairs = list(pairs or [])

        def get_list(self, name):
            low = name.lower()
            return [v for k, v in self._pairs if k.lower() == low]

        def get(self, name, default=None):
            values = self.get_list(name)
            return values[-1] if values else default

    class FakeResponse:
        """最小响应对象，模拟 httpx2.Response 的接口。"""

        def __init__(self, status_code=200, text="", payload=None, set_cookies=None):
            self.status_code = status_code
            self.text = text
            self._payload = payload
            pairs = [("set-cookie", c) for c in (set_cookies or [])]
            self.headers = FakeHeaders(pairs)

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    class AsyncRequestUtils:
        """按 URL 注册响应的假客户端，供远程模式测试使用。"""

        # url 子串 -> 响应；同时记录所有请求便于断言。
        routes = {}
        requests = []

        async def get_res(self, url=None, headers=None, **kwargs):
            AsyncRequestUtils.requests.append(("GET", url, headers))
            for key, resp in AsyncRequestUtils.routes.items():
                if key in (url or ""):
                    return resp
            return FakeResponse(404, "")

        async def post_res(self, url=None, headers=None, json=None, data=None, **kwargs):
            AsyncRequestUtils.requests.append(("POST", url, json if json is not None else data))
            for key, resp in AsyncRequestUtils.routes.items():
                if key in (url or ""):
                    return resp
            return FakeResponse(404, "")

    network_mod.AsyncRequestUtils = AsyncRequestUtils
    # 供测试构造响应对象使用
    network_mod._FakeResponse = FakeResponse
    network_mod._FakeHeaders = FakeHeaders

    # app.schemas.types
    schemas = types.ModuleType("app.schemas")
    types_mod = types.ModuleType("app.schemas.types")

    class MediaType:
        MOVIE = "电影"
        TV = "电视剧"
        MUSIC = "音乐"

    class MediaSource(str, _Enum):
        """与宿主一致：MediaSource 是 str 枚举，.value 为 "themoviedb"。"""

        TMDB = "themoviedb"

    class EventType:
        PluginAction = "plugin.action"

    class MessageType:
        Plugin = "插件"

    types_mod.MediaType = MediaType
    types_mod.MediaSource = MediaSource
    types_mod.EventType = EventType
    types_mod.MessageType = MessageType

    # app.chain.*
    chain = types.ModuleType("app.chain.media")
    chain_sub = types.ModuleType("app.chain.subscribe")

    class MediaChain:
        pass

    class SubscribeChain:
        """记录 add 调用，返回可配置结果。"""

        add_calls = []
        add_result = (1, "新增订阅成功")

        def add(self, **kwargs):
            SubscribeChain.add_calls.append(kwargs)
            return SubscribeChain.add_result

    chain.MediaChain = MediaChain
    chain_sub.SubscribeChain = SubscribeChain

    # app.db.oper.subscribe
    db = types.ModuleType("app.db")
    db_oper = types.ModuleType("app.db.oper")
    db_oper_sub = types.ModuleType("app.db.oper.subscribe")

    class SubscribeOper:
        """订阅 Oper 替身。"""

        existing = set()
        rows = []
        deleted = []

        @staticmethod
        def _norm_source(value):
            """
            与宿主一致地规范化媒体来源。

            真实 SubscribeOper 的 _lookup_values 会把枚举取出 value 再用作查询参数；
            这里必须照做，否则测试用 str(枚举) 得到 'MediaSource.TMDB' 而永远无法命中，
            会掩盖插件里来源传递的错误。
            """
            if value is None:
                return None
            return getattr(value, "value", value)

        def exists(self, media_source=None, media_id=None, **kwargs):
            key = (self._norm_source(media_source), str(media_id))
            return key in SubscribeOper.existing

        def list_by_media_identity(self, media_source=None, media_id=None, **kwargs):
            want_source = self._norm_source(media_source)
            want_id = str(media_id)
            return [
                r for r in SubscribeOper.rows
                if self._norm_source(getattr(r, "media_source", None)) == want_source
                and str(getattr(r, "media_id", "")) == want_id
            ]

        def list(self, state=None):
            return list(SubscribeOper.rows)

        def delete(self, sid):
            SubscribeOper.deleted.append(sid)

    db_oper_sub.SubscribeOper = SubscribeOper

    # apscheduler
    apscheduler = types.ModuleType("apscheduler")
    trig = types.ModuleType("apscheduler.triggers")
    trig_cron = types.ModuleType("apscheduler.triggers.cron")

    class CronTrigger:
        @staticmethod
        def from_crontab(expr):
            fields = expr.split()
            if len(fields) != 5:
                raise ValueError("invalid cron")
            return {"cron": expr}

    trig_cron.CronTrigger = CronTrigger

    modules = {
        "app": app,
        "app.sdk": sdk,
        "app.sdk.logging": logging_mod,
        "app.sdk.plugin": plugin_mod,
        "app.sdk.network": network_mod,
        "app.schemas": schemas,
        "app.schemas.types": types_mod,
        "app.chain": types.ModuleType("app.chain"),
        "app.chain.media": chain,
        "app.chain.subscribe": chain_sub,
        "app.db": db,
        "app.db.oper": db_oper,
        "app.db.oper.subscribe": db_oper_sub,
        "apscheduler": apscheduler,
        "apscheduler.triggers": trig,
        "apscheduler.triggers.cron": trig_cron,
    }
    sys.modules.update(modules)
    app.sdk = sdk


_install_stubs()

# 插件使用 `from .client import ...`，因此必须按「包」加载：
# 先建一个包对象并登记到 sys.modules，再加载子模块与主模块。
_PKG_NAME = "mediaautomatorsync_under_test"
_pkg = types.ModuleType(_PKG_NAME)
_pkg.__path__ = [str(PLUGIN_INIT.parent)]
sys.modules[_PKG_NAME] = _pkg

_client_spec = importlib.util.spec_from_file_location(
    f"{_PKG_NAME}.client", PLUGIN_INIT.parent / "client.py"
)
client_module = importlib.util.module_from_spec(_client_spec)
sys.modules[f"{_PKG_NAME}.client"] = client_module
_client_spec.loader.exec_module(client_module)

_spec = importlib.util.spec_from_file_location(f"{_PKG_NAME}.__init__", PLUGIN_INIT)
plugin_module = importlib.util.module_from_spec(_spec)
sys.modules[f"{_PKG_NAME}.__init__"] = plugin_module
_spec.loader.exec_module(plugin_module)
MediaAutomatorSync = plugin_module.MediaAutomatorSync
MediaAutomatorClient = client_module.MediaAutomatorClient
_network_mod = sys.modules["app.sdk.network"]
FakeAsyncRequestUtils = _network_mod.AsyncRequestUtils
FakeResponse = _network_mod._FakeResponse


def _ts(minutes_ago: int = 5) -> str:
    """生成相对当前时间的时间戳。

    插件按 ``max_age_days`` 过滤陈旧日志，因此测试不能写死日期，
    否则用例会随着真实时间推移而失效。
    """
    import datetime

    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@pytest.fixture()
def plugin():
    """返回一份启用状态、默认配置的插件实例。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": "unused.log"})
    return instance


# --------------------------------------------------------------------------
# 日志解析
# --------------------------------------------------------------------------

def test_parse_json_line():
    """JSON 行应被解析为结构化记录并保留原始文本。"""
    # 私有方法在模块内以名称改写形式存在，测试中按改写名调用。
    instance = MediaAutomatorSync()
    record = instance._MediaAutomatorSync__parse_log_line(
        json.dumps(
            {
                "timestamp": _ts(5),
                "level": "INFO",
                "log_module": "telegram",
                "message": "订阅成功",
            },
            ensure_ascii=False,
        )
    )
    assert record is not None
    assert record["level"] == "INFO"
    assert record["message"] == "订阅成功"


def test_parse_plaintext_line_falls_back():
    """非 JSON 行（配置加载失败时的兜底格式）仍应可解析。"""
    instance = MediaAutomatorSync()
    record = instance._MediaAutomatorSync__parse_log_line(
        "[INFO] [2025-01-01 10:00:00] media_automator - 订阅成功"
    )
    assert record is not None
    assert "订阅成功" in record["message"]


def test_parse_blank_line():
    """空白行应被忽略。"""
    instance = MediaAutomatorSync()
    assert instance._MediaAutomatorSync__parse_log_line("   ") is None


# --------------------------------------------------------------------------
# 事件分类
# --------------------------------------------------------------------------

def test_classify_add_by_message(plugin):
    """文案命中新增关键词时判定为 add。"""
    record = {"message": "订阅成功：黑袍纠察队", "timestamp": _ts(5)}
    assert plugin._MediaAutomatorSync__classify(record) == "add"


def test_classify_delete_by_message(plugin):
    """文案命中删除关键词时判定为 delete。"""
    record = {"message": "已删除订阅：黑袍纠察队", "timestamp": _ts(5)}
    assert plugin._MediaAutomatorSync__classify(record) == "delete"


def test_classify_delete_wins_over_add(plugin):
    """同时命中两种关键词时删除优先，避免误把删除当新增。"""
    record = {"message": "删除订阅（此前曾订阅成功）：某剧", "timestamp": _ts(5)}
    assert plugin._MediaAutomatorSync__classify(record) == "delete"


def test_classify_by_event_type(plugin):
    """event_type 是比中文文案更稳定的判据。"""
    add_record = {
        "message": "操作完成",
        "event_type": "subscription_add_success",
        "timestamp": _ts(5),
    }
    del_record = {
        "message": "操作完成",
        "event_type": "subscription_deleted",
        "timestamp": _ts(5),
    }
    assert plugin._MediaAutomatorSync__classify(add_record) == "add"
    assert plugin._MediaAutomatorSync__classify(del_record) == "delete"


def test_classify_ignores_unrelated(plugin):
    """无关日志不应被识别为订阅事件。"""
    record = {"message": "TG 频道消息已缓存", "timestamp": _ts(5)}
    assert plugin._MediaAutomatorSync__classify(record) is None


def test_classify_respects_max_age(plugin):
    """超出保留天数的旧日志应被跳过。"""
    plugin._max_age_days = 1
    record = {"message": "订阅成功", "timestamp": _ts(60 * 24 * 400)}
    assert plugin._MediaAutomatorSync__classify(record) is None


# --------------------------------------------------------------------------
# 媒体信息提取
# --------------------------------------------------------------------------

def test_extract_media_from_context(plugin):
    """结构化 context 优先于文本正则。"""
    record = {
        "message": "订阅成功",
        "context": {"title": "黑袍纠察队", "year": 2019, "tmdb_id": 76479, "type": "tv"},
    }
    media = plugin._MediaAutomatorSync__extract_media(record)
    assert media["title"] == "黑袍纠察队"
    assert media["media_id"] == "76479"
    assert media["year"] == "2019"
    assert media["raw_type"] == "tv"
    assert media["media_source"] == "themoviedb"


def test_extract_media_from_message(plugin):
    """没有结构化字段时从 message 中正则提取。"""
    record = {"message": "订阅成功：《沙丘2》(2024) TMDB ID: 693134"}
    media = plugin._MediaAutomatorSync__extract_media(record)
    assert media["media_id"] == "693134"
    assert media["title"] == "沙丘2"
    assert media["year"] == "2024"


def test_extract_media_year_from_title_text(plugin):
    """标题写法的年份应能被提取。"""
    record = {"message": "已删除订阅：黑袍纠察队,2019"}
    media = plugin._MediaAutomatorSync__extract_media(record)
    assert media["year"] == "2019"


def test_resolve_media_type(plugin):
    """类型字段与兜底配置应正确映射到 MediaType。"""
    assert plugin._MediaAutomatorSync__resolve_media_type({"raw_type": "movie"}, {}) == "电影"
    assert plugin._MediaAutomatorSync__resolve_media_type({"raw_type": "tv"}, {}) == "电视剧"
    # auto 模式返回 None，交给 MoviePilot 按标题识别。
    assert plugin._MediaAutomatorSync__resolve_media_type({"raw_type": ""}, {}) is None
    plugin._default_type = "movie"
    assert plugin._MediaAutomatorSync__resolve_media_type({"raw_type": ""}, {}) == "电影"


# --------------------------------------------------------------------------
# 端到端同步（使用临时日志文件）
# --------------------------------------------------------------------------

def _write_log(path: Path, records):
    """把记录写成 JSON 行日志。"""
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_sync_add_and_delete(tmp_path):
    """一次同步应同时处理新增与删除，并推进读取位置。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [
            {
                "timestamp": _ts(5),
                "level": "INFO",
                "message": "订阅成功",
                "context": {"title": "沙丘2", "year": 2024, "tmdb_id": 693134, "type": "movie"},
            },
            {
                "timestamp": _ts(4),
                "level": "INFO",
                "message": "已删除订阅",
                "context": {"title": "某剧", "tmdb_id": 111, "type": "tv"},
            },
        ],
    )

    from app.db.oper.subscribe import SubscribeOper

    # 为删除路径准备一条 MoviePilot 订阅。
    row = types.SimpleNamespace(id=42, name="某剧", media_source="themoviedb", media_id="111")
    SubscribeOper.rows = [row]
    SubscribeOper.deleted = []
    SubscribeOper.existing = set()

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (7, "新增订阅成功")

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    result = instance.sync()

    assert result["success"] is True
    assert result["added"] == 1
    assert result["deleted"] == 1
    assert SubscribeOper.deleted == [42]
    assert SubscribeChain.add_calls[0]["media_id"] == "693134"

    # 游标应推进到文件末尾。
    assert instance._cursor["offset"] == log_file.stat().st_size

    # 再次同步不应重复处理。
    SubscribeChain.add_calls = []
    SubscribeOper.deleted = []
    second = instance.sync()
    assert second["added"] == 0
    assert second["deleted"] == 0


def test_sync_handles_rotation(tmp_path):
    """日志轮转后应从头读取新文件，而不是继续用旧偏移量。"""
    log_file = tmp_path / "app.log"
    # 首次写入较多内容，使文件明显大于轮转后的新文件。
    _write_log(
        log_file,
        [
            {"timestamp": _ts(5), "message": "订阅成功", "context": {"tmdb_id": 1}},
            {"timestamp": _ts(5), "message": "无关日志", "context": {}},
            {"timestamp": _ts(5), "message": "无关日志", "context": {}},
            {"timestamp": _ts(5), "message": "无关日志", "context": {}},
        ],
    )

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_result = (1, "ok")
    SubscribeChain.add_calls = []
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    first = instance.sync()
    assert first["added"] == 1
    old_offset = instance._cursor["offset"]

    # 模拟轮转：新文件内容更短，偏移量已超出文件大小。
    _write_log(
        log_file,
        [{"timestamp": _ts(3), "message": "订阅成功", "context": {"tmdb_id": 2}}],
    )
    assert log_file.stat().st_size < old_offset, "测试前提：新文件必须小于旧偏移量"

    SubscribeChain.add_calls = []
    second = instance.sync()
    assert second["added"] == 1
    assert SubscribeChain.add_calls[0]["media_id"] == "2"


def test_sync_disabled_plugin(tmp_path):
    """未启用时不应执行任何同步。"""
    log_file = tmp_path / "app.log"
    _write_log(log_file, [{"message": "订阅成功", "context": {"tmdb_id": 1}}])
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": False, "log_path": str(log_file)})
    result = instance.sync()
    assert result["success"] is False
    assert result["added"] == 0


def test_sync_missing_log_file(tmp_path):
    """日志文件不存在时应返回错误而不是抛异常。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(tmp_path / "nope.log")})
    result = instance.sync()
    assert result["success"] is False
    assert "不存在" in result["message"]


def test_add_failure_falls_back_to_title(tmp_path):
    """TMDB 身份订阅失败时应回退按标题识别。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [
            {
                "timestamp": _ts(5),
                "message": "订阅成功",
                "context": {"title": "沙丘2", "tmdb_id": 693134, "type": "movie"},
            }
        ],
    )

    from app.chain.subscribe import SubscribeChain

    calls = {"n": 0}

    def _add(self, **kwargs):
        # 插件通过 SubscribeChain() 实例调用，因此这里是实例方法（需要 self）。
        calls["n"] += 1
        SubscribeChain.add_calls.append(kwargs)
        # 第一次（带 TMDB 身份）失败，第二次（按标题）成功。
        return (None, "未识别到媒体信息") if calls["n"] == 1 else (9, "新增订阅成功")

    original_add = SubscribeChain.add
    SubscribeChain.add = _add
    SubscribeChain.add_calls = []

    try:
        instance = MediaAutomatorSync()
        instance.init_plugin({"enabled": True, "log_path": str(log_file)})
        result = instance.sync()
    finally:
        # 还原类属性，避免影响后续用例。
        SubscribeChain.add = original_add

    assert result["added"] == 1
    assert len(SubscribeChain.add_calls) == 2
    # 第二次回退调用不应再带 TMDB 身份。
    assert SubscribeChain.add_calls[1].get("media_source") is None


def test_delete_by_title_when_no_tmdb_id(tmp_path):
    """没有 TMDB ID 时按标题包含匹配删除，与 MA 语义一致。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [{"timestamp": _ts(5), "message": "已删除订阅：黑袍纠察队"}],
    )

    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = [
        types.SimpleNamespace(id=1, name="黑袍纠察队 第一季"),
        types.SimpleNamespace(id=2, name="其它剧"),
    ]
    SubscribeOper.deleted = []

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    result = instance.sync()
    assert result["deleted"] == 1
    assert SubscribeOper.deleted == [1]


def test_sync_respects_toggles(tmp_path):
    """关闭同步开关后对应方向的事件应被跳过。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [
            {"timestamp": _ts(5), "message": "订阅成功", "context": {"tmdb_id": 1}},
            {"timestamp": _ts(4), "message": "已删除订阅", "context": {"tmdb_id": 2}},
        ],
    )
    instance = MediaAutomatorSync()
    instance.init_plugin(
        {"enabled": True, "log_path": str(log_file), "sync_add": False, "sync_delete": False}
    )
    result = instance.sync()
    assert result["added"] == 0
    assert result["deleted"] == 0
    assert result["skipped"] == 2


def test_invalid_cron_returns_no_service():
    """非法 cron 表达式不应注册服务，也不应抛异常。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "cron": "not a cron", "log_path": "x.log"})
    assert instance.get_service() == []


def test_parse_timestamp_variants():
    """日志时间戳的多种写法都应解析成功，非法值返回 None。"""
    parse = MediaAutomatorSync._MediaAutomatorSync__parse_timestamp
    assert parse("2025-10-28 12:00:00") is not None
    assert parse("2025-10-28 12:00:00,123") is not None
    assert parse("2025-10-28 12:00:00.123456") is not None
    assert parse("2025-10-28T12:00:00.123456") is not None
    # 带时区偏移
    assert parse("2025-10-28T12:00:00+08:00") is not None
    # log() 会给 message 追加 " | k=v" 后缀，时间戳字段也可能带上，必须仍可解析
    parsed = parse("2025-10-28 12:00:00 | k=v, k2=v2")
    assert parsed is not None
    assert parsed.strftime("%Y-%m-%d %H:%M:%S") == "2025-10-28 12:00:00"
    # 非法输入不应抛异常
    assert parse("not a timestamp") is None
    assert parse("") is None
    assert parse(None) is None


def test_invalid_regex_falls_back_to_substring(plugin):
    """用户填错正则时退化为子串匹配，不应抛异常。"""
    # 故意写一个非法正则（未闭合的括号）
    plugin._add_patterns = "(未闭合"
    assert plugin._MediaAutomatorSync__matches("订阅成功", plugin._add_patterns) is False
    # 非法正则但文本包含字面量时仍应命中
    assert plugin._MediaAutomatorSync__matches("前面(未闭合 后面", plugin._add_patterns) is True


def test_get_state_reflects_config():
    """get_state 应反映启用开关。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": False})
    assert instance.get_state() is False
    instance.init_plugin({"enabled": True})
    assert instance.get_state() is True


def test_init_plugin_is_repeatable():
    """重复初始化不应累加状态，也不应丢失已保存的历史。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": "a.log"})
    instance._MediaAutomatorSync__record_history("add", {"title": "x"}, "ok")
    assert len(instance._history) == 1

    # 再次初始化（宿主保存配置时会重入）应读回已有历史而不是叠加。
    instance.init_plugin({"enabled": True, "log_path": "b.log"})
    assert len(instance._history) == 1
    assert instance._log_path == "b.log"


# --------------------------------------------------------------------------
# 远程 HTTP 模式
# --------------------------------------------------------------------------

BASE = "http://192.168.6.2:8010"


def _reset_routes():
    """清空假 HTTP 客户端注册的路由与请求记录。"""
    FakeAsyncRequestUtils.routes = {}
    FakeAsyncRequestUtils.requests = []


def _log_text(records):
    """把记录拼成下载端点会返回的日志全文。"""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def _http_plugin(**overrides):
    """构造一个使用远程模式的插件实例。"""
    config = {
        "enabled": True,
        "log_source": "http",
        "ma_url": BASE,
        "ma_username": "root",
        "ma_password": "secret",
        "cron": "*/2 * * * *",
    }
    config.update(overrides)
    instance = MediaAutomatorSync()
    instance.init_plugin(config)
    return instance


def test_client_extract_token_variants():
    """登录响应的多种 Token 结构都应能被识别。"""
    pick = MediaAutomatorClient._extract_token
    assert pick({"access_token": "a"}) == "a"
    assert pick({"token": "b"}) == "b"
    assert pick({"jwt_token": "c"}) == "c"
    assert pick({"data": {"access_token": "d"}}) == "d"
    assert pick({"result": {"token": "e"}}) == "e"
    # 没有 Token 时返回 None，不应误取其它字段
    assert pick({"message": "ok"}) is None
    assert pick("not a dict") is None


def test_client_available_requires_config():
    """缺少地址或凭证时 available() 应为 False。"""
    assert MediaAutomatorClient(base_url="", username="root", password="x").available() is False
    assert MediaAutomatorClient(base_url=BASE, username="", password="").available() is False
    # 只有 Token 也算可用
    assert MediaAutomatorClient(base_url=BASE, token="t").available() is True
    assert MediaAutomatorClient(base_url=BASE, username="root", password="p").available() is True


def test_remote_sync_adds_subscription():
    """远程模式：登录后拉取日志并添加订阅。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (11, "新增订阅成功")

    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(
            200, "", {"access_token": "tok-123"}
        ),
        "/api/system/logs/download": FakeResponse(
            200,
            _log_text(
                [
                    {
                        "timestamp": _ts(3),
                        "message": "订阅成功",
                        "context": {"title": "沙丘2", "tmdb_id": 693134, "type": "movie"},
                    }
                ]
            ),
        ),
    }

    instance = _http_plugin()
    result = instance.sync()

    assert result["success"] is True, result
    assert result["added"] == 1
    assert SubscribeChain.add_calls[0]["media_id"] == "693134"
    # 登录得到 Token 后，下载请求应带上 Authorization
    download_calls = [r for r in FakeAsyncRequestUtils.requests if "logs/download" in (r[1] or "")]
    assert download_calls, "应请求日志下载端点"
    assert download_calls[0][2].get("Authorization") == "Bearer tok-123"


def test_remote_sync_does_not_reprocess_same_log():
    """远程模式每次返回全文，重复轮询不应重复处理。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    text = _log_text(
        [{"timestamp": _ts(3), "message": "订阅成功", "context": {"tmdb_id": 111}}]
    )
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, text),
    }

    instance = _http_plugin()
    first = instance.sync()
    assert first["added"] == 1

    # 同一份日志再拉一次：应识别为没有新增行
    SubscribeChain.add_calls = []
    second = instance.sync()
    assert second["added"] == 0
    assert second["scanned"] == 0


def test_remote_sync_picks_up_appended_lines():
    """日志追加新行后，只处理新增部分。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    first_records = [{"timestamp": _ts(3), "message": "订阅成功", "context": {"tmdb_id": 1}}]
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(first_records)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 1

    # 追加一条新记录后重新拉取
    second_records = first_records + [
        {"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 2}}
    ]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(
        200, _log_text(second_records)
    )
    SubscribeChain.add_calls = []
    result = instance.sync()

    assert result["scanned"] == 1, result
    assert result["added"] == 1
    assert SubscribeChain.add_calls[0]["media_id"] == "2"


def test_remote_sync_handles_log_rotation():
    """远程日志被清理/轮转（行数减少）时应重新按最近行处理。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    many = [{"timestamp": _ts(5), "message": "无关日志", "context": {}} for _ in range(5)]
    many.append({"timestamp": _ts(4), "message": "订阅成功", "context": {"tmdb_id": 1}})
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(many)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 1

    # 日志被清理成只剩两行，含一条新的订阅事件
    rotated = [{"timestamp": _ts(1), "message": "订阅成功", "context": {"tmdb_id": 9}}]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(
        200, _log_text(rotated)
    )
    SubscribeChain.add_calls = []
    result = instance.sync()
    assert result["added"] == 1
    assert SubscribeChain.add_calls[0]["media_id"] == "9"


def test_remote_login_discovery_via_openapi():
    """未配置登录路径时，应从 openapi.json 发现登录端点。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeAsyncRequestUtils.routes = {
        "openapi.json": FakeResponse(
            200, "", {"paths": {"/api/system/auth/signin": {}, "/api/other": {}}}
        ),
        "/api/system/auth/signin": FakeResponse(
            200, "", {"data": {"access_token": "discovered"}}
        ),
        "/logs/download": FakeResponse(
            200, _log_text([{"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 5}}])
        ),
    }
    instance = _http_plugin()
    result = instance.sync()
    assert result["added"] == 1
    # 目标登录端点确实被调用过
    login_calls = [r for r in FakeAsyncRequestUtils.requests if "signin" in (r[1] or "")]
    assert login_calls, "应调用从 openapi 发现的登录端点"


def test_remote_reports_error_when_unreachable():
    """服务不可达时应返回失败并给出错误，不应抛异常。"""
    _reset_routes()
    instance = _http_plugin(ma_url=BASE)
    result = instance.sync()
    assert result["success"] is False
    assert result["message"]
    assert instance._last_error


def test_remote_requires_credentials():
    """既没 Token 也没账号密码时应报明确错误。"""
    _reset_routes()
    instance = _http_plugin(ma_username="", ma_password="", ma_token="")
    result = instance.sync()
    assert result["success"] is False
    assert "未配置" in result["message"] or "登录" in result["message"]


def test_remote_sends_cookie_from_set_cookie():
    """
    Media Automator 的下载接口只认登录下发的 cookie，不认 Authorization 头。

    这是线上实测得到的真实行为：登录响应形如
        {"access_token": "eyJ...", "token_type": "bearer"}
        Set-Cookie: ma_<hash>="Bearer eyJ..."; HttpOnly; Path=/
    下载接口必须带上该 cookie 才能通过鉴权，因此这里断言 cookie 被保存并回传。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    cookie_name = "ma_285446e3"
    cookie_value = '"Bearer eyJhbGciOiJIUzI1NiJ9.fake"'
    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(
            200,
            "",
            {"access_token": "tok-abc", "token_type": "bearer"},
            set_cookies=[f"{cookie_name}={cookie_value}; HttpOnly; Max-Age=604800; Path=/; SameSite=lax"],
        ),
        "/logs/download": FakeResponse(
            200,
            _log_text([{"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 3}}]),
        ),
    }

    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.sync()
    assert result["added"] == 1, result

    download_calls = [r for r in FakeAsyncRequestUtils.requests if "logs/download" in (r[1] or "")]
    assert download_calls, "应请求日志下载端点"
    sent_headers = download_calls[0][2] or {}
    # 关键断言：带上从 Set-Cookie 收集到的会话 cookie
    assert cookie_name in (sent_headers.get("Cookie") or ""), sent_headers
    assert "Bearer" in (sent_headers.get("Cookie") or ""), sent_headers


def test_remote_clears_stale_cookie_on_401():
    """
    401 后重试必须清除旧 cookie。

    线上出现过「登录成功但一直 401」的循环：旧 cookie 未清除，重试仍带着失效凭证。
    这里验证重新登录后会使用新 cookie。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    state = {"n": 0}
    cookie_name = "ma_aaaa1111"

    class _FlakyUtils(FakeAsyncRequestUtils):
        async def get_res(self, url=None, headers=None, **kwargs):
            FakeAsyncRequestUtils.requests.append(("GET", url, headers))
            if "logs/download" in (url or ""):
                state["n"] += 1
                if state["n"] == 1:
                    return FakeResponse(401, "")
                # 第二次必须已经换成新 cookie
                assert "fresh" in ((headers or {}).get("Cookie") or ""), headers
                return FakeResponse(
                    200,
                    _log_text([{"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 8}}]),
                )
            return FakeResponse(404, "")

        async def post_res(self, url=None, headers=None, json=None, data=None, **kwargs):
            FakeAsyncRequestUtils.requests.append(("POST", url, json or data))
            return FakeResponse(
                200,
                "",
                {"access_token": "fresh-token"},
                set_cookies=[f'{cookie_name}="Bearer fresh"; HttpOnly; Path=/'],
            )

    _network_mod.AsyncRequestUtils = _FlakyUtils
    try:
        instance = _http_plugin(ma_login_path="/auth/token")
        result = instance.sync()
    finally:
        _network_mod.AsyncRequestUtils = FakeAsyncRequestUtils

    assert result["added"] == 1, result
    assert state["n"] == 2, "应在 401 后重试一次"


def test_remote_login_uses_form_encoding():
    """
    登录必须用表单体而非 JSON。

    Media Automator 的 /auth/token 是 FastAPI 的 OAuth2PasswordRequestForm，
    发 JSON 会被判为缺少参数并返回 422。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/logs/download": FakeResponse(
            200, _log_text([{"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 4}}])
        ),
    }
    instance = _http_plugin(ma_login_path="/auth/token")
    instance.sync()

    login_calls = [r for r in FakeAsyncRequestUtils.requests if "auth/token" in (r[1] or "")]
    assert login_calls, "应调用登录端点"
    # 记录的是 json or data；表单体应为 dict 且含 username/password
    body = login_calls[0][2]
    assert isinstance(body, dict), body
    assert body.get("username") == "root"
    assert body.get("password") == "secret"


def test_remote_retries_after_401():
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    # 用一个可变的响应对象模拟「第一次 401，重新登录后成功」
    state = {"count": 0}

    class _FlakyUtils(FakeAsyncRequestUtils):
        async def get_res(self, url=None, headers=None, **kwargs):
            FakeAsyncRequestUtils.requests.append(("GET", url, headers))
            if "logs/download" in (url or ""):
                state["count"] += 1
                if state["count"] == 1:
                    return FakeResponse(401, "")
                return FakeResponse(
                    200,
                    _log_text([{"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 7}}]),
                )
            # openapi 探测不应命中（返回 404 让客户端走内置候选路径）
            return FakeResponse(404, "")

        async def post_res(self, url=None, headers=None, json=None, data=None, **kwargs):
            # 登录走 POST，这里必须一并覆盖，否则会命中空路由表返回 404。
            FakeAsyncRequestUtils.requests.append(("POST", url, json or data))
            return FakeResponse(200, "", {"token": "fresh"})

    _network_mod.AsyncRequestUtils = _FlakyUtils
    try:
        instance = _http_plugin(ma_token="stale-token")
        result = instance.sync()
    finally:
        _network_mod.AsyncRequestUtils = FakeAsyncRequestUtils

    assert result["added"] == 1, result
    assert state["count"] == 2, "应在 401 后重试一次"


# --------------------------------------------------------------------------
# 查缺补漏（全量订阅比对）
# --------------------------------------------------------------------------

# 取自实测的 /api/subscriptions 响应结构（精简）。
SUBSCRIPTIONS_PAYLOAD = {
    "tv": {
        "items": [
            {"id": 37, "tmdb_id": 294446, "media_type": "tv", "title": "美人余", "year": "2026"},
            {"id": 14, "tmdb_id": 45790, "media_type": "tv", "title": "JOJO的奇妙冒险", "year": "2012"},
        ],
        "total_items": 2,
    },
    "movie": {
        "items": [
            {"id": 25, "tmdb_id": 1368337, "media_type": "movie", "title": "奥德赛", "year": "2026"},
        ],
        "total_items": 1,
    },
    "collection": {"items": [], "total_items": 0},
}


def test_client_fetch_subscriptions_flattens_groups():
    """订阅清单应把 tv/movie/collection 三组拍平成统一记录。"""
    _reset_routes()
    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(200, "", SUBSCRIPTIONS_PAYLOAD),
    }
    import asyncio

    client = MediaAutomatorClient(base_url=BASE, username="root", password="secret", login_path="/auth/token")
    records = asyncio.run(client.fetch_subscriptions())
    assert records is not None
    assert len(records) == 3
    by_id = {r["tmdb_id"]: r for r in records}
    assert by_id["294446"]["title"] == "美人余"
    assert by_id["294446"]["media_type"] == "tv"
    assert by_id["1368337"]["media_type"] == "movie"
    # 空分组不应产生记录
    assert "collection" not in {r["group"] for r in records}


def test_client_fetch_subscriptions_skips_incomplete():
    """缺少 tmdb_id 或标题的条目应被跳过，而不是让整份清单失败。"""
    _reset_routes()
    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(
            200,
            "",
            {
                "tv": {
                    "items": [
                        {"id": 1, "tmdb_id": None, "title": "无ID"},
                        {"id": 2, "tmdb_id": 123, "title": None},
                        {"id": 3, "tmdb_id": 456, "title": "正常", "year": "2024"},
                    ]
                },
                "movie": {"items": []},
            },
        ),
    }
    import asyncio

    client = MediaAutomatorClient(base_url=BASE, username="root", password="secret", login_path="/auth/token")
    records = asyncio.run(client.fetch_subscriptions())
    assert records is not None
    assert [r["tmdb_id"] for r in records] == ["456"]


def test_reconcile_adds_only_missing():
    """查缺补漏只补 MP 缺的，已有的不重复添加。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    # MP 已有 JOJO（45790），缺美人余（294446）和奥德赛（1368337）
    SubscribeOper.existing = {("themoviedb", "45790")}
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(200, "", SUBSCRIPTIONS_PAYLOAD),
    }

    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile()

    assert result["success"] is True, result
    assert "MA 共 3 条" in result["message"]
    assert "已有 1 条" in result["message"]
    # 只应提交缺失的两条
    added_ids = sorted(c["media_id"] for c in SubscribeChain.add_calls)
    assert added_ids == ["1368337", "294446"], added_ids


def test_reconcile_dry_run_does_not_write():
    """dry_run 只列出待补条目，不实际写入订阅。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(200, "", SUBSCRIPTIONS_PAYLOAD),
    }
    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile(dry_run=True)

    assert result["success"] is True
    assert "待补 3 条" in result["message"]
    assert SubscribeChain.add_calls == [], "dry_run 不应写入订阅"
    assert len(result["data"]) == 3


def test_reconcile_respects_limit():
    """limit 限制单次补订数量。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(200, "", SUBSCRIPTIONS_PAYLOAD),
    }
    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile(limit=1)

    assert result["success"] is True
    assert len(SubscribeChain.add_calls) == 1, SubscribeChain.add_calls


def test_reconcile_requires_http_mode():
    """文件模式没有订阅清单接口，应给出明确提示。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    result = instance.api_reconcile()
    assert result["success"] is False
    assert "远程 HTTP" in result["message"]


def test_reconcile_reports_error_when_unreachable():
    """拉取订阅清单失败时应返回失败而不是抛异常。"""
    _reset_routes()
    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile()
    assert result["success"] is False
    assert result["message"]
