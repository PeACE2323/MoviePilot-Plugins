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

    class Response:
        """
        与宿主一致的统一响应 envelope。

        插件 API 走宿主统一客户端时必须是这个三段式结构；返回裸字典会被前端判为
        「服务器返回了无效响应」。测试桩保留同名属性，插件里写错属性名会立即报错。
        """

        def __init__(self, success=False, message="", data=None):
            self.success = success
            self.message = message
            self.data = data

        def model_dump(self):
            return {"success": self.success, "message": self.message, "data": self.data}

    schemas.Response = Response

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
        """
        MediaChain 替身，用于返回季列表。

        真实 MediaInfo.seasons 是 Dict[int, list]（季号 -> 该季集列表）；
        recognize_media 查不到时返回 None，插件必须容忍这一点。
        """

        # tmdb_id -> 季号列表；未登记的返回 None（模拟识别失败）
        seasons_by_id = {}

        def recognize_media(self, **kwargs):
            media_id = str(kwargs.get("media_id") or "")
            seasons = MediaChain.seasons_by_id.get(media_id)
            if seasons is None:
                return None
            return types.SimpleNamespace(seasons={s: [] for s in seasons})

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
            """
            与宿主一致地按身份 + 季号判断订阅是否存在。

            真实 SubscribeOper.exists 接受 season 参数（订阅是按季去重的），
            忽略它会让「逐季订阅」的测试永远认为不存在，从而掩盖重复订阅的问题。
            """
            season = kwargs.get("season")
            key = (self._norm_source(media_source), str(media_id), season)
            if key in SubscribeOper.existing:
                return True
            # 兼容不关心季号的旧用例：只给 (source, id) 也算命中
            return (self._norm_source(media_source), str(media_id)) in SubscribeOper.existing

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

        def list_by_username(self, username, state=None, mtype=None):
            """
            与宿主一致：按订阅用户筛选。

            清理端点靠这一条区分「插件建的订阅」与「用户手动建的订阅」，
            替身里若忽略 username 过滤，就会把用户自己的订阅也列进待删名单，
            测试反而会掩盖最危险的那种误删。
            """
            return [
                r for r in SubscribeOper.rows
                if getattr(r, "username", None) == username
            ]

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
FakeMediaChain = sys.modules["app.chain.media"].MediaChain


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


def _append_log(path: Path, records):
    """向 JSON 行日志追加记录，模拟运行期产生的新事件。"""
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _prime_file(tmp_path: Path, **overrides):
    """
    建一份只含陈旧记录的日志并同步一次，得到「已建立基线」的插件实例。

    v1.5.0 起首次同步只记录读取位置与水位线、不执行任何历史事件——这正是误同步
    事故（MA 上只订阅了《女士的法则》，MP 里却多出整个暮光之城系列）的修复点。
    因此凡是要验证「新事件会被处理」的用例，都必须先建立基线再追加新行。

    :param tmp_path: 用例专属临时目录
    :param overrides: 覆盖默认配置
    :return: (插件实例, 日志路径)
    """
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [{"timestamp": _ts(120), "level": "INFO", "message": "服务启动"}],
    )
    config = {"enabled": True, "log_path": str(log_file)}
    config.update(overrides)
    instance = MediaAutomatorSync()
    instance.init_plugin(config)
    baseline = instance.sync()
    assert baseline["added"] == 0 and baseline["deleted"] == 0, baseline
    return instance, log_file


def test_sync_add_and_delete(tmp_path):
    """一次同步应同时处理新增与删除，并推进读取位置。"""
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    instance, log_file = _prime_file(tmp_path)

    # 为删除路径准备一条 MoviePilot 订阅。
    row = types.SimpleNamespace(id=42, name="某剧", media_source="themoviedb", media_id="111")
    SubscribeOper.rows = [row]
    SubscribeOper.deleted = []
    SubscribeOper.existing = set()

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (7, "新增订阅成功")

    _append_log(
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
    # 先写满一份日志并同步一次，建立偏移量与水位线。
    _write_log(
        log_file,
        [
            {"timestamp": _ts(120), "message": "服务启动"},
            {"timestamp": _ts(120), "message": "无关日志", "context": {}},
            {"timestamp": _ts(120), "message": "无关日志", "context": {}},
            {"timestamp": _ts(120), "message": "无关日志", "context": {}},
        ],
    )

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_result = (1, "ok")
    SubscribeChain.add_calls = []
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    first = instance.sync()
    # 首次只建立基线，日志里的历史事件一律不执行。
    assert first["added"] == 0, first
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
    from app.chain.subscribe import SubscribeChain

    instance, log_file = _prime_file(tmp_path)

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
        _append_log(
            log_file,
            [
                {
                    "timestamp": _ts(5),
                    "message": "订阅成功",
                    "context": {"title": "沙丘2", "tmdb_id": 693134, "type": "movie"},
                }
            ],
        )
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
    from app.db.oper.subscribe import SubscribeOper

    instance, log_file = _prime_file(tmp_path)

    SubscribeOper.rows = [
        types.SimpleNamespace(id=1, name="黑袍纠察队 第一季"),
        types.SimpleNamespace(id=2, name="其它剧"),
    ]
    SubscribeOper.deleted = []

    _append_log(
        log_file,
        [{"timestamp": _ts(5), "message": "已删除订阅：黑袍纠察队"}],
    )
    result = instance.sync()
    assert result["deleted"] == 1
    assert SubscribeOper.deleted == [1]


def test_sync_respects_toggles(tmp_path):
    """关闭同步开关后对应方向的事件应被跳过。"""
    instance, log_file = _prime_file(
        tmp_path, sync_add=False, sync_delete=False
    )
    _append_log(
        log_file,
        [
            {"timestamp": _ts(5), "message": "订阅成功", "context": {"tmdb_id": 1}},
            {"timestamp": _ts(4), "message": "已删除订阅", "context": {"tmdb_id": 2}},
        ],
    )
    result = instance.sync()
    assert result["added"] == 0
    assert result["deleted"] == 0
    assert result["skipped"] == 2


# --------------------------------------------------------------------------
# 误同步防护（v1.5.0）
#
# 线上事故：MA 里只订阅了《女士的法则》，插件首次运行却把最近 30 天日志里的
# 所有「新增订阅」全部重放了一遍，MoviePilot 里凭空多出整个暮光之城系列。
# 下面这些用例锁住修复点：首次只建基线、基线可跨重启、水位线挡住陈旧行、
# 批内去重、脏标题拒收。
# --------------------------------------------------------------------------

def test_first_run_only_builds_baseline(tmp_path):
    """首次同步绝不能把日志里的历史订阅当成新事件执行。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [
            {
                "timestamp": _ts(30),
                "message": "订阅成功",
                "context": {"title": "暮光之城", "tmdb_id": 111, "type": "movie"},
            },
            {
                "timestamp": _ts(20),
                "message": "订阅成功",
                "context": {"title": "暮光之城2：新月", "tmdb_id": 222, "type": "movie"},
            },
        ],
    )

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    result = instance.sync()

    assert result["success"] is True
    assert result["added"] == 0
    assert SubscribeChain.add_calls == [], "历史订阅不应被重放"
    assert "基线" in result["message"]


def test_baseline_survives_reinit(tmp_path):
    """基线应落盘：宿主重入 init_plugin 后不得重新处理历史事件。"""
    log_file = tmp_path / "app.log"
    _write_log(
        log_file,
        [
            {
                "timestamp": _ts(30),
                "message": "订阅成功",
                "context": {"title": "暮光之城", "tmdb_id": 111, "type": "movie"},
            }
        ],
    )

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    assert instance.sync()["added"] == 0

    # 宿主保存配置时会重建插件实例，游标与水位线必须还在。
    restarted = MediaAutomatorSync()
    restarted.init_plugin({"enabled": True, "log_path": str(log_file)})
    SubscribeChain.add_calls = []
    again = restarted.sync()

    assert again["added"] == 0
    assert SubscribeChain.add_calls == []


def test_watermark_blocks_stale_lines_after_rotation(tmp_path):
    """轮转后重扫最近行时，比基线更旧的事件必须被水位线挡住。"""
    _reset_routes()
    log_file = tmp_path / "app.log"
    # 基线文件写厚一点，让下一份更小的日志触发「文件变小 = 轮转」这条路径。
    _write_log(
        log_file,
        [{"timestamp": _ts(300), "message": "服务启动"} for _ in range(7)]
        + [{"timestamp": _ts(60), "message": "服务启动"}],
    )

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_path": str(log_file)})
    assert instance.sync()["added"] == 0

    # 整份换成「一条比基线更旧的事件 + 一条新事件」，字节数变小触发重扫。
    _write_log(
        log_file,
        [
            {
                "timestamp": _ts(120),
                "message": "订阅成功",
                "context": {"title": "暮光之城", "tmdb_id": 111, "type": "movie"},
            },
            {
                "timestamp": _ts(2),
                "message": "订阅成功",
                "context": {"title": "女士的法则", "tmdb_id": 333, "type": "movie"},
            },
        ],
    )
    assert log_file.stat().st_size < instance._cursor["offset"]
    result = instance.sync()

    assert result["stale"] == 1, result
    assert result["added"] == 1
    assert [call["media_id"] for call in SubscribeChain.add_calls] == ["333"]


def test_duplicate_lines_in_one_batch_are_merged(tmp_path):
    """MA 为同一次订阅写多行日志，只应产生一次订阅。"""
    instance, log_file = _prime_file(tmp_path)

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    _append_log(
        log_file,
        [
            {
                "timestamp": _ts(4),
                "message": "已添加订阅",
                "context": {"title": "美人余", "tmdb_id": 294446, "type": "movie"},
            },
            {
                "timestamp": _ts(3),
                "message": "发送消息：title='美人余 (2026) 已添加订阅'",
            },
            {"timestamp": _ts(2), "message": "订阅成功：美人余"},
        ],
    )
    result = instance.sync()

    assert result["added"] == 1, result
    assert result["duplicate"] >= 1
    assert len(SubscribeChain.add_calls) == 1


def test_title_suffix_is_stripped(plugin):
    """标题里混入的「(2026) 已添加订阅」应被剥掉，只留片名。"""
    media = plugin._MediaAutomatorSync__extract_media(
        {"message": "发送消息：title='美人余 (2026) 已添加订阅'", "timestamp": _ts(5)}
    )
    assert media["title"] == "美人余"


def test_dirty_title_is_rejected(plugin):
    """抽到的是日志正文而不是片名时必须拒收，不能拿去识别。"""
    extract = plugin._MediaAutomatorSync__extract_media

    # 结构化文本片段
    assert extract({"message": "订阅成功：level=INFO title=美人余"})["title"] is None
    # 超长正文
    assert extract({"message": "订阅成功：" + "很长的日志正文" * 20})["title"] is None
    # 纯数字
    assert extract({"message": "订阅成功：12345"})["title"] is None


def test_history_card_shows_source_log_line(tmp_path):
    """订错片时要能一眼看出是哪一行日志触发的。"""
    instance, log_file = _prime_file(tmp_path)

    from app.chain.subscribe import SubscribeChain

    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    _append_log(
        log_file,
        [
            {
                "timestamp": _ts(2),
                "message": "订阅成功",
                "context": {"title": "女士的法则", "tmdb_id": 294446, "type": "movie"},
            }
        ],
    )
    assert instance.sync()["added"] == 1

    joined = "\n".join(_collect_texts(instance.get_page()))
    assert "日志：" in joined, joined
    assert "女士的法则" in joined


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
    # 季列表也一并清空，避免用例之间互相影响
    FakeMediaChain.seasons_by_id = {}


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

    # 下载端点每次返回整份日志，第一次拉取只用来建立基线。
    history = [
        {
            "timestamp": _ts(60),
            "message": "订阅成功",
            "context": {"title": "旧剧", "tmdb_id": 1, "type": "tv"},
        }
    ]
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(
            200, "", {"access_token": "tok-123"}
        ),
        "/api/system/logs/download": FakeResponse(200, _log_text(history)),
    }

    instance = _http_plugin()
    baseline = instance.sync()
    assert baseline["added"] == 0, baseline

    # 追加一条新事件后再次同步，才应真正添加订阅。
    fresh = history + [
        {
            "timestamp": _ts(3),
            "message": "订阅成功",
            "context": {"title": "沙丘2", "tmdb_id": 693134, "type": "movie"},
        }
    ]
    FakeAsyncRequestUtils.routes["/api/system/logs/download"] = FakeResponse(
        200, _log_text(fresh)
    )
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

    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(history)),
    }

    instance = _http_plugin()
    assert instance.sync()["added"] == 0  # 建立基线

    fresh = history + [
        {"timestamp": _ts(3), "message": "订阅成功", "context": {"tmdb_id": 111}}
    ]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(200, _log_text(fresh))
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

    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(history)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 0  # 建立基线

    # 追加一条新记录后重新拉取
    second_records = history + [
        {"timestamp": _ts(3), "message": "订阅成功", "context": {"tmdb_id": 2}}
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

    many = [{"timestamp": _ts(120), "message": "无关日志", "context": {}} for _ in range(5)]
    many.append({"timestamp": _ts(60), "message": "订阅成功", "context": {"tmdb_id": 1}})
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(many)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 0  # 建立基线

    # 日志被清理成只剩一行，含一条新的订阅事件
    rotated = [{"timestamp": _ts(1), "message": "订阅成功", "context": {"tmdb_id": 9}}]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(
        200, _log_text(rotated)
    )
    SubscribeChain.add_calls = []
    result = instance.sync()
    assert result["added"] == 1
    assert SubscribeChain.add_calls[0]["media_id"] == "9"


def test_rotation_without_watermark_only_rebuilds_baseline():
    """旧版本升级上来时只有行计数、没有水位线，遇到轮转不能重放历史事件。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    many = [{"timestamp": _ts(120), "message": "无关日志", "context": {}} for _ in range(5)]
    many.append({"timestamp": _ts(60), "message": "订阅成功", "context": {"tmdb_id": 1}})
    FakeAsyncRequestUtils.routes = {
        "/api/login": FakeResponse(200, "", {"token": "t"}),
        "/logs/download": FakeResponse(200, _log_text(many)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 0  # 建立基线

    # 模拟从旧版本升级：行计数还在，但水位线这个新字段是空的。
    instance._watermark = ""

    rotated = [{"timestamp": _ts(30), "message": "订阅成功", "context": {"tmdb_id": 7}}]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(
        200, _log_text(rotated)
    )
    SubscribeChain.add_calls = []
    result = instance.sync()

    assert result["added"] == 0
    assert SubscribeChain.add_calls == [], "缺水位线时不得执行任何历史事件"
    assert "基线" in result["message"]


def test_remote_login_discovery_via_openapi():
    """未配置登录路径时，应从 openapi.json 发现登录端点。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = []
    SubscribeOper.existing = set()
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    FakeAsyncRequestUtils.routes = {
        "openapi.json": FakeResponse(
            200, "", {"paths": {"/api/system/auth/signin": {}, "/api/other": {}}}
        ),
        "/api/system/auth/signin": FakeResponse(
            200, "", {"data": {"access_token": "discovered"}}
        ),
        "/logs/download": FakeResponse(200, _log_text(history)),
    }
    instance = _http_plugin()
    assert instance.sync()["added"] == 0  # 建立基线

    fresh = history + [
        {"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 5}}
    ]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(200, _log_text(fresh))
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
    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(
            200,
            "",
            {"access_token": "tok-abc", "token_type": "bearer"},
            set_cookies=[f"{cookie_name}={cookie_value}; HttpOnly; Max-Age=604800; Path=/; SameSite=lax"],
        ),
        "/logs/download": FakeResponse(200, _log_text(history)),
    }

    instance = _http_plugin(ma_login_path="/auth/token")
    assert instance.sync()["added"] == 0  # 建立基线

    fresh = history + [
        {"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 3}}
    ]
    FakeAsyncRequestUtils.routes["/logs/download"] = FakeResponse(200, _log_text(fresh))
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
    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    fresh = history + [
        {"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 8}}
    ]

    class _FlakyUtils(FakeAsyncRequestUtils):
        async def get_res(self, url=None, headers=None, **kwargs):
            FakeAsyncRequestUtils.requests.append(("GET", url, headers))
            if "logs/download" not in (url or ""):
                return FakeResponse(404, "")
            state["n"] += 1
            if state["n"] == 1:
                return FakeResponse(401, "")
            # 401 之后的每次请求都必须已经换成重新登录拿到的 cookie
            assert "fresh" in ((headers or {}).get("Cookie") or ""), headers
            # 重试那次只建立基线，第二次同步才带回新事件
            return FakeResponse(200, _log_text(history if state["n"] == 2 else fresh))

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
        baseline = instance.sync()
        assert baseline["added"] == 0, baseline
        result = instance.sync()
    finally:
        _network_mod.AsyncRequestUtils = FakeAsyncRequestUtils

    assert result["added"] == 1, result
    assert state["n"] == 3, "首次应 401 后重试一次，随后再正常拉取一次"


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
    history = [{"timestamp": _ts(60), "message": "服务启动"}]
    fresh = history + [
        {"timestamp": _ts(2), "message": "订阅成功", "context": {"tmdb_id": 7}}
    ]

    class _FlakyUtils(FakeAsyncRequestUtils):
        async def get_res(self, url=None, headers=None, **kwargs):
            FakeAsyncRequestUtils.requests.append(("GET", url, headers))
            if "logs/download" in (url or ""):
                state["count"] += 1
                if state["count"] == 1:
                    return FakeResponse(401, "")
                return FakeResponse(
                    200, _log_text(history if state["count"] == 2 else fresh)
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
        # 第一次同步：401 → 重新登录 → 重试成功，只用来建立基线
        assert instance.sync()["added"] == 0
        result = instance.sync()
    finally:
        _network_mod.AsyncRequestUtils = FakeAsyncRequestUtils

    assert result["added"] == 1, result
    assert state["count"] == 3, "首次应 401 后重试一次，随后再正常拉取一次"


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

    assert result.success is True, result
    assert "MA 共 3 条" in result.message
    assert "已有 1 条" in result.message
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

    assert result.success is True
    assert "待补 3 条" in result.message
    assert SubscribeChain.add_calls == [], "dry_run 不应写入订阅"
    assert len(result.data) == 3


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

    assert result.success is True
    assert len(SubscribeChain.add_calls) == 1, SubscribeChain.add_calls


def test_reconcile_requires_http_mode():
    """文件模式没有订阅清单接口，应给出明确提示。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    result = instance.api_reconcile()
    assert result.success is False
    assert "远程 HTTP" in result.message


def test_reconcile_reports_error_when_unreachable():
    """拉取订阅清单失败时应返回失败而不是抛异常。"""
    _reset_routes()
    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile()
    assert result.success is False
    assert result.message


# --------------------------------------------------------------------------
# 详情页操作按钮
# --------------------------------------------------------------------------

def _collect_buttons(node, found=None):
    """递归收集页面 JSON 里的所有 VBtn。"""
    if found is None:
        found = []
    if isinstance(node, dict):
        if node.get("component") == "VBtn":
            found.append(node)
        for value in node.values():
            _collect_buttons(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_buttons(value, found)
    return found


def _collect_texts(node, found=None):
    """递归收集页面 JSON 里所有的 text 字段，用于断言页面到底显示了什么。"""
    if found is None:
        found = []
    if isinstance(node, dict):
        value = node.get("text")
        if isinstance(value, str):
            found.append(value)
        for item in node.values():
            _collect_texts(item, found)
    elif isinstance(node, list):
        for item in node:
            _collect_texts(item, found)
    return found


def test_page_exposes_action_buttons_in_http_mode():
    """HTTP 模式详情页应提供同步、测试连接与查缺补漏按钮，且 API 路径正确。"""
    instance = _http_plugin()
    buttons = _collect_buttons(instance.get_page())
    calls = {}
    for btn in buttons:
        click = (btn.get("events") or {}).get("click") or {}
        calls[btn.get("text")] = (click.get("api"), click.get("params") or {})

    # 路径必须遵循 plugin/<PluginID>/<path> 约定，否则前端调用会 404
    assert calls["立即同步一次"][0] == "plugin/MediaAutomatorSync/run"
    assert calls["测试连接"][0] == "plugin/MediaAutomatorSync/test"
    assert calls["查缺补漏（预演）"][0] == "plugin/MediaAutomatorSync/reconcile"
    # 预演必须带 dry_run，避免误触发真实写入
    assert calls["查缺补漏（预演）"][1].get("dry_run") is True
    assert calls["查缺补漏（补齐订阅）"][0] == "plugin/MediaAutomatorSync/reconcile"
    assert "dry_run" not in calls["查缺补漏（补齐订阅）"][1]


def test_page_hides_reconcile_buttons_in_file_mode():
    """文件模式无法读取 MA 订阅清单，查缺补漏按钮应隐藏。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    texts = [btn.get("text") for btn in _collect_buttons(instance.get_page())]
    assert "立即同步一次" in texts
    assert "测试连接" in texts
    assert not any("查缺补漏" in (t or "") for t in texts)


def test_every_page_action_targets_a_registered_api():
    """
    详情页里每个按钮调用的路径都必须存在于 get_api() 声明中。

    这是防止「按钮点了 404」的关键：路径写错时插件加载正常，只有用户点下去才暴露。
    """
    instance = _http_plugin()
    declared = {
        f"plugin/MediaAutomatorSync{api['path']}"
        for api in instance.get_api()
    }
    for btn in _collect_buttons(instance.get_page()):
        click = (btn.get("events") or {}).get("click") or {}
        api_path = click.get("api")
        if not api_path:
            continue
        # 调用的 path 可能带 query，这里只比对路径部分
        assert api_path in declared, f"按钮「{btn.get('text')}」指向未声明的 API：{api_path}"


def test_api_endpoints_declare_response_model():
    """
    每个 API 都必须声明 response_model。

    详情页按钮走宿主统一客户端，它按标准三段式解析；不声明模型时前端会把响应
    当成无效数据，表现为「服务器返回了无效响应」。
    """
    instance = _http_plugin()
    for api in instance.get_api():
        assert api.get("response_model") is not None, f"{api['path']} 缺少 response_model"


def test_api_methods_return_response_envelope():
    """
    API 方法必须返回 schemas.Response，而不是裸字典。

    裸字典在插件加载和单测里都看不出问题，只有真实点击按钮才会暴露。
    """
    from app.schemas import Response

    _reset_routes()
    instance = _http_plugin(ma_login_path="/auth/token")

    # 不可达场景：应返回 Response 且 success 为 False
    result = instance.api_reconcile()
    assert isinstance(result, Response), type(result)
    assert result.success is False

    # 文件模式的 test 接口同样要返回 Response
    file_instance = MediaAutomatorSync()
    file_instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "nope.log"})
    assert isinstance(file_instance.api_test(), Response)
    assert isinstance(file_instance.api_reset(), Response)
    assert isinstance(file_instance.api_status(), Response)
    assert isinstance(file_instance.api_run(), Response)

    # 查缺补漏在文件模式下应明确拒绝，而不是抛异常
    rejected = file_instance.api_reconcile()
    assert isinstance(rejected, Response)
    assert rejected.success is False
    assert "远程 HTTP" in rejected.message


def test_reconcile_result_is_visible_after_page_reload():
    """
    手动操作的结果必须能在详情页看到。

    详情页按钮的机制是「调接口 → 重新加载页面数据」，接口的返回值不会被渲染。
    因此结果必须落盘到插件数据，否则用户点击后只会看到进度条闪一下，以为没反应。
    """
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
    instance.api_reconcile(dry_run=True)

    # 重新加载页面（模拟前端点击后的刷新）
    texts = _collect_texts(instance.get_page())
    joined = "\n".join(texts)
    assert "查缺补漏（预演）" in joined, joined
    assert "MA 共 3 条订阅" in joined, joined
    # 明细里应逐条列出待补的媒体
    assert "美人余" in joined, joined


def test_action_result_survives_reinit():
    """操作结果写入插件存储，宿主重入 init_plugin 后仍应在详情页可见。"""
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
    instance.api_reconcile(dry_run=True)

    # 宿主保存配置/更新插件时会重入 init_plugin，结果不应丢失
    instance.init_plugin(
        {
            "enabled": True,
            "log_source": "http",
            "ma_url": BASE,
            "ma_username": "root",
            "ma_password": "secret",
        }
    )
    joined = "\n".join(_collect_texts(instance.get_page()))
    assert "查缺补漏（预演）" in joined, joined


# --------------------------------------------------------------------------
# 电视剧多季订阅
# --------------------------------------------------------------------------

def test_tv_subscription_expands_all_seasons():
    """
    多季电视剧必须逐季订阅。

    Media Automator 订阅的是整部剧，而 MoviePilot 的 SubscribeChain.add 在
    不传 season 时只订第 1 季；不展开会让多季剧只订到第一季。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    # JOJO：6 季
    FakeMediaChain.seasons_by_id = {"45790": [1, 2, 3, 4, 5, 6]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "JOJO的奇妙冒险", "year": "2012", "media_id": "45790", "raw_type": "tv"},
        {},
    )

    assert outcome["changed"] is True, outcome
    seasons = sorted(c.get("season") for c in SubscribeChain.add_calls)
    assert seasons == [1, 2, 3, 4, 5, 6], seasons
    # 每一季都必须带上 TMDB 身份
    assert all(c.get("media_id") == "45790" for c in SubscribeChain.add_calls)
    assert all(c.get("mtype") == "电视剧" for c in SubscribeChain.add_calls)


def test_single_season_tv_uses_single_subscription():
    """单季剧不应被拆成多条订阅。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    FakeMediaChain.seasons_by_id = {"294446": [1]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "美人余", "year": "2026", "media_id": "294446", "raw_type": "tv"},
        {},
    )

    assert outcome["changed"] is True
    assert len(SubscribeChain.add_calls) == 1, SubscribeChain.add_calls
    # 单季走普通路径，不应显式传 season
    assert SubscribeChain.add_calls[0].get("season") is None


def test_tv_season_lookup_failure_falls_back():
    """查不到季列表时应退回单条订阅，而不是直接失败。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    # 未登记 -> recognize_media 返回 None
    FakeMediaChain.seasons_by_id = {}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "某剧", "year": "2024", "media_id": "999", "raw_type": "tv"},
        {},
    )

    assert outcome["changed"] is True, outcome
    assert len(SubscribeChain.add_calls) == 1
    assert SubscribeChain.add_calls[0].get("media_id") == "999"


def test_tv_seasons_skip_already_subscribed():
    """已订阅的季应跳过，只补缺的季。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    # 已有 1、2 季
    SubscribeOper.existing = {("themoviedb", "45790", 1), ("themoviedb", "45790", 2)}
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "新增订阅成功")

    FakeMediaChain.seasons_by_id = {"45790": [1, 2, 3]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "JOJO的奇妙冒险", "year": "2012", "media_id": "45790", "raw_type": "tv"},
        {},
    )

    assert outcome["changed"] is True
    assert [c.get("season") for c in SubscribeChain.add_calls] == [3]
    assert "已有 2 季" in outcome["message"]
    assert "新增 1 季" in outcome["message"]


def test_tv_all_seasons_present_reports_no_change():
    """所有季都已订阅时不应产生变更，避免重复通知。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = {("themoviedb", "45790", s) for s in (1, 2, 3)}
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeMediaChain.seasons_by_id = {"45790": [1, 2, 3]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "JOJO的奇妙冒险", "year": "2012", "media_id": "45790", "raw_type": "tv"},
        {},
    )

    assert outcome["changed"] is False
    assert SubscribeChain.add_calls == []
    assert "已有 3 季" in outcome["message"]


def test_movie_is_not_season_expanded():
    """电影不应走分季逻辑。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    # 即便误配了季列表，电影也不该展开
    FakeMediaChain.seasons_by_id = {"1368337": [1, 2, 3]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "奥德赛", "year": "2026", "media_id": "1368337", "raw_type": "movie"},
        {},
    )

    assert outcome["changed"] is True
    assert len(SubscribeChain.add_calls) == 1
    assert SubscribeChain.add_calls[0].get("season") is None


def test_format_seasons_compacts_ranges():
    """季号展示应压缩连续区间。"""
    fmt = MediaAutomatorSync._MediaAutomatorSync__format_seasons
    assert fmt([1, 2, 3]) == "S1-S3"
    assert fmt([1]) == "S1"
    assert fmt([1, 2, 4, 5, 6]) == "S1-S2,S4-S6"
    assert fmt([2, 1]) == "S1-S2"
    assert fmt([]) == ""


def test_specials_included_by_default():
    """默认包含季 0（特别篇）。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeMediaChain.seasons_by_id = {"100": [0, 1, 2]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    instance._MediaAutomatorSync__apply_add(
        {"title": "某剧", "year": "2024", "media_id": "100", "raw_type": "tv"}, {}
    )

    seasons = sorted(c.get("season") for c in SubscribeChain.add_calls)
    assert seasons == [0, 1, 2], seasons


def test_specials_can_be_excluded():
    """关闭开关后不订阅季 0，只订正季。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeMediaChain.seasons_by_id = {"100": [0, 1, 2]}

    instance = MediaAutomatorSync()
    instance.init_plugin(
        {
            "enabled": True,
            "log_source": "file",
            "log_path": "x.log",
            "include_specials": False,
        }
    )
    instance._MediaAutomatorSync__apply_add(
        {"title": "某剧", "year": "2024", "media_id": "100", "raw_type": "tv"}, {}
    )

    seasons = sorted(c.get("season") for c in SubscribeChain.add_calls)
    assert seasons == [1, 2], seasons


def test_specials_only_show_still_subscribes():
    """只有特别篇的剧（仅季 0）在开启开关时仍应订阅，而不是被整体跳过。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeMediaChain.seasons_by_id = {"200": [0]}

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    outcome = instance._MediaAutomatorSync__apply_add(
        {"title": "特别篇合集", "year": "2024", "media_id": "200", "raw_type": "tv"}, {}
    )

    assert outcome["changed"] is True
    assert [c.get("season") for c in SubscribeChain.add_calls] == [0]


def test_include_specials_defaults_to_true():
    """未配置时默认包含特别篇。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    assert instance._include_specials is True


# --------------------------------------------------------------------------
# 洗版开关
# --------------------------------------------------------------------------

def test_best_version_not_forced_by_default():
    """
    默认不干预洗版：完全不传 best_version，沿用 MoviePilot 的默认订阅规则。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    instance._MediaAutomatorSync__apply_add(
        {"title": "沙丘2", "year": "2024", "media_id": "693134", "raw_type": "movie"}, {}
    )

    assert len(SubscribeChain.add_calls) == 1
    assert "best_version" not in SubscribeChain.add_calls[0]


def test_force_no_best_version_marks_subscription_as_normal():
    """
    开启开关后必须显式传 best_version=False。

    否则若主程序默认规则或洗版插件把新订阅设为洗版，首次同步的剧没有任何可比较的
    已入库版本，就会陷入「一直洗版、从不下载」——这正是线上实际发生的现象。
    """
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    instance = MediaAutomatorSync()
    instance.init_plugin(
        {
            "enabled": True,
            "log_source": "file",
            "log_path": "x.log",
            "force_no_best_version": True,
        }
    )
    instance._MediaAutomatorSync__apply_add(
        {"title": "沙丘2", "year": "2024", "media_id": "693134", "raw_type": "movie"}, {}
    )

    call = SubscribeChain.add_calls[0]
    assert call.get("best_version") is False, call
    assert call.get("best_version_full") is False, call


def test_force_no_best_version_applies_to_each_season():
    """逐季订阅时每一季都要带上洗版开关，漏一季就会留下洗版订阅。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeMediaChain.seasons_by_id = {"45790": [1, 2, 3]}

    instance = MediaAutomatorSync()
    instance.init_plugin(
        {
            "enabled": True,
            "log_source": "file",
            "log_path": "x.log",
            "force_no_best_version": True,
        }
    )
    instance._MediaAutomatorSync__apply_add(
        {"title": "JOJO的奇妙冒险", "year": "2012", "media_id": "45790", "raw_type": "tv"}, {}
    )

    assert len(SubscribeChain.add_calls) == 3
    for call in SubscribeChain.add_calls:
        assert call.get("best_version") is False, call


def test_force_no_best_version_defaults_to_false():
    """默认不强制关闭洗版，避免覆盖用户自己的订阅偏好。"""
    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    assert instance._force_no_best_version is False


def test_force_no_best_version_exposed_in_form():
    """配置页必须提供该开关，否则用户无法启用。"""
    instance = MediaAutomatorSync()
    form, default = instance.get_form()
    models = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("props") or {}
            if isinstance(props, dict) and "model" in props:
                models.append(props["model"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(form)
    assert "force_no_best_version" in models
    assert default.get("force_no_best_version") is False


def test_reconcile_expands_tv_seasons():
    """查缺补漏对多季剧同样要逐季补齐。"""
    _reset_routes()
    from app.chain.subscribe import SubscribeChain
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.existing = set()
    SubscribeOper.rows = []
    SubscribeChain.add_calls = []
    SubscribeChain.add_result = (1, "ok")

    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(
            200,
            "",
            {
                "tv": {
                    "items": [
                        {"id": 1, "tmdb_id": 45790, "media_type": "tv", "title": "JOJO", "year": "2012"}
                    ],
                    "total_items": 1,
                },
                "movie": {"items": []},
            },
        ),
    }
    FakeMediaChain.seasons_by_id = {"45790": [1, 2, 3]}

    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_reconcile()

    assert result.success is True, result.message
    seasons = sorted(c.get("season") for c in SubscribeChain.add_calls)
    assert seasons == [1, 2, 3], seasons


# --------------------------------------------------------------------------
# 清理多余订阅
# --------------------------------------------------------------------------

def _sub(sid, name, media_id=None, username="Media Automator", year=None, season=None):
    """构造一条订阅行，字段与真实 Subscribe 模型对齐。"""
    return types.SimpleNamespace(
        id=sid,
        name=name,
        media_id=media_id,
        username=username,
        year=year,
        season=season,
    )


def _purge_plugin(rows):
    """装好订阅行与 MA 清单路由，返回远程模式插件实例。"""
    _reset_routes()
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = rows
    SubscribeOper.deleted = []
    SubscribeOper.existing = set()
    FakeAsyncRequestUtils.routes = {
        "/auth/token": FakeResponse(200, "", {"access_token": "t"}),
        "/api/subscriptions": FakeResponse(200, "", SUBSCRIPTIONS_PAYLOAD),
    }
    return _http_plugin(ma_login_path="/auth/token")


def test_purge_dry_run_lists_but_does_not_delete():
    """预演只列出待清理项，一条都不删。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin(
        [
            _sub(1, "美人余", media_id="294446", year="2026"),  # MA 里还在
            _sub(2, "暮光之城"),  # 误同步出来的
            _sub(3, "女士的法则"),  # 误同步出来的
        ]
    )
    result = instance.api_purge(dry_run=True)

    assert result.success is True, result.message
    assert "待清理 2 条" in result.message
    assert SubscribeOper.deleted == [], "预演不应删除任何订阅"
    assert len(result.data) == 2


def test_purge_keeps_subscriptions_still_in_ma():
    """MA 清单里仍然存在的订阅必须保留，不能被清理掉。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin(
        [
            _sub(1, "美人余", media_id="294446", year="2026"),
            _sub(2, "JOJO的奇妙冒险", media_id="45790"),
            _sub(3, "奥德赛", media_id="1368337"),
            _sub(4, "暮光之城"),
        ]
    )
    result = instance.api_purge(dry_run=False)

    assert result.success is True, result.message
    assert SubscribeOper.deleted == [4], SubscribeOper.deleted
    assert "已清理 1 条" in result.message


def test_purge_matches_ma_by_title_when_id_missing():
    """没有 TMDB ID 的订阅按标题核对，标题在 MA 清单里就保留。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin([_sub(1, "美人余"), _sub(2, "不存在的剧")])
    instance.api_purge(dry_run=False)

    assert SubscribeOper.deleted == [2], SubscribeOper.deleted


def test_purge_never_touches_user_created_subscriptions():
    """只清理插件建的订阅，用户自己手动建的订阅一律不碰。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin(
        [
            _sub(1, "用户自己订的剧", username=None),
            _sub(2, "用户用别的用户名订的剧", username="someone"),
            _sub(3, "误同步出来的剧"),
        ]
    )
    instance.api_purge(dry_run=False)

    assert SubscribeOper.deleted == [3], SubscribeOper.deleted


def test_purge_filters_by_title_keywords():
    """titles 参数只清理命中的标题，其余保留。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin(
        [
            _sub(1, "暮光之城"),
            _sub(2, "暮光之城2：新月"),
            _sub(3, "女士的法则"),
        ]
    )
    result = instance.api_purge(dry_run=False, titles="暮光之城")

    assert SubscribeOper.deleted == [1, 2], SubscribeOper.deleted
    assert "已清理 2 条" in result.message


def test_purge_accepts_full_width_comma_in_titles():
    """中文逗号分隔的关键字同样生效。"""
    from app.db.oper.subscribe import SubscribeOper

    instance = _purge_plugin(
        [_sub(1, "暮光之城"), _sub(2, "女士的法则"), _sub(3, "别的剧")]
    )
    instance.api_purge(dry_run=False, titles="暮光之城，女士的法则")

    assert sorted(SubscribeOper.deleted) == [1, 2], SubscribeOper.deleted


def test_purge_requires_http_mode_when_checking_ma():
    """文件模式拿不到 MA 清单，核对模式下必须拒绝执行而不是盲删。"""
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = [_sub(1, "某剧")]
    SubscribeOper.deleted = []

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    result = instance.api_purge(dry_run=False, keep_ma=True)

    assert result.success is False
    assert "远程 HTTP" in result.message
    assert SubscribeOper.deleted == [], "核对不可用时应拒绝删除"


def test_purge_allows_explicit_no_cross_check_in_file_mode():
    """显式关闭核对时，文件模式也能按用户名清理。"""
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = [_sub(1, "某剧"), _sub(2, "用户剧", username=None)]
    SubscribeOper.deleted = []

    instance = MediaAutomatorSync()
    instance.init_plugin({"enabled": True, "log_source": "file", "log_path": "x.log"})
    result = instance.api_purge(dry_run=False, keep_ma=False)

    assert result.success is True, result.message
    assert SubscribeOper.deleted == [1], SubscribeOper.deleted


def test_purge_aborts_when_ma_unreachable():
    """拉不到 MA 清单时必须中止，不能当成「MA 里都没有」而全删。"""
    _reset_routes()
    from app.db.oper.subscribe import SubscribeOper

    SubscribeOper.rows = [_sub(1, "某剧")]
    SubscribeOper.deleted = []

    instance = _http_plugin(ma_login_path="/auth/token")
    result = instance.api_purge(dry_run=False)

    assert result.success is False
    assert SubscribeOper.deleted == [], "清单拉取失败时不应删除"


def test_purge_result_is_visible_after_page_reload():
    """接口返回值不会被详情页渲染，结果必须落盘才能在页面上看到。"""
    instance = _purge_plugin([_sub(1, "暮光之城"), _sub(2, "美人余", media_id="294446")])
    instance.api_purge(dry_run=True)

    last_action = instance.get_data("last_action")
    assert last_action is not None
    assert last_action["title"] == "清理本插件订阅（预演）"
    assert any("暮光之城" in line for line in last_action["details"])


def test_page_exposes_purge_buttons():
    """详情页要有清理按钮，且预演与删除走同一个端点、参数不同。"""
    instance = _purge_plugin([])
    buttons = _collect_buttons(instance.get_page())
    purge_buttons = [
        b for b in buttons
        if ((b.get("events") or {}).get("click") or {}).get("api")
        == "plugin/MediaAutomatorSync/purge"
    ]
    assert len(purge_buttons) == 2, [b.get("text") for b in purge_buttons]
    params = [
        ((b["events"]["click"].get("params")) or {}).get("dry_run")
        for b in purge_buttons
    ]
    assert sorted(params, key=str) == [False, True], params


def test_purge_describes_season_in_details():
    """多季订阅在明细里要标出季号，便于用户确认删的是哪一条。"""
    instance = _purge_plugin([_sub(1, "某剧", season=2, year="2024")])
    result = instance.api_purge(dry_run=True)

    assert "第 2 季" in result.data[0]

