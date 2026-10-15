"""Media Automator 订阅同步插件。

读取 Media Automator 的 ``app.log``，在 MoviePilot 侧镜像它的订阅变更：日志里出现
新增订阅就把对应媒体加入 MoviePilot 订阅，出现删除订阅就把 MoviePilot 上的同一条
订阅删掉。

Media Automator 的日志由 ``app/core/logging.py`` 里的 ``CustomJsonFormatter``
写出，每行是一个独立的 JSON 对象，至少包含 ``timestamp`` / ``level`` /
``log_module`` / ``message``，可能还带 ``event_type`` / ``action`` / ``status``
和结构化 ``context``。本插件据此按行解析 JSON，并用可配置的正则从 ``message``
与 ``context`` 中提取媒体类型、TMDB ID、标题和年份。

日志有两种来源（``log_source`` 配置项）：

* ``file``：读取共享文件（需要把日志目录挂载进 MoviePilot 容器），用文件偏移量
  做增量；
* ``http``：通过 Media Automator 的 Web 接口登录后下载日志全文
  （``GET /api/system/logs/download``），不需要任何挂载；因为该接口每次返回整份
  文件，增量由「行内容指纹」去重实现。

设计要点：

* 两种来源都只处理新增内容，重复轮询不会重复添加订阅；
* 变更处理后推进状态并落盘，崩溃或重启不会丢失待同步事件；
* 通过 MoviePilot 的 ``SubscribeChain`` 完成识别与写入，不直接操作宿主 Model；
* 所有网络与数据库动作都在插件定时服务里执行，不在导入期或类定义期发生。
"""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.db.oper.subscribe import SubscribeOper
from app.schemas.types import MediaSource, MediaType
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .client import LOG_DOWNLOAD_PATH, MediaAutomatorClient

# 默认日志路径。Media Automator 在容器内把日志固定在 /data/logs/app.log，
# 但它和 MoviePilot 通常是两个不同的容器，所以插件挂载后的实际路径要由用户配置。
DEFAULT_LOG_PATH = "/data/logs/app.log"

# 日志来源：读取共享文件 / 通过 HTTP 拉取。
LOG_SOURCE_FILE = "file"
LOG_SOURCE_HTTP = "http"

# 默认的“新增订阅”识别正则。命中 message 或 context 文本即认为是新增事件。
# 说明：Media Automator 的业务代码以编译产物分发，无法从源码逐一核对文案，
# 因此这里给出一组覆盖常见写法的保守默认值，并允许用户在插件设置里改写。
DEFAULT_ADD_PATTERNS = r"订阅成功|添加订阅成功|已添加订阅|新增订阅|订阅已创建|已创建订阅|订阅成功添加"
# 默认的“删除订阅”识别正则。
DEFAULT_DELETE_PATTERNS = r"删除订阅|已删除订阅|取消订阅|移除订阅|订阅已删除|删除成功.*订阅"

# 从文本中提取 TMDB ID 的正则，支持 "TMDB ID: 12345"、"tmdb_id=12345"、"TMDB:12345"。
TMDB_ID_RE = re.compile(r"tmdb[_\s-]*(?:id)?\s*[:=]\s*(\d+)", re.IGNORECASE)
# 退化的纯数字 TMDB ID 写法，例如 "订阅成功，TMDB 12345"。
TMDB_BARE_RE = re.compile(r"tmdb\D{0,10}(\d{4,})", re.IGNORECASE)
# 提取年份，如 "2024" / "(2024)" / "，2019"。
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 时间戳主体前缀，用于从可能带后缀的字符串中截出可解析的部分。
TIMESTAMP_PREFIX_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2})?")
# 提取中文/英文标题的通用模式，优先匹配书名号或引号包裹的内容。
QUOTED_TITLE_RE = re.compile(r"[《「『\"'“”](.+?)[》」』\"'“”]")
# “关键词：标题”式标题，例如“已删除订阅：黑袍纠察队”“订阅成功 - 沙丘2”。
# 关键词后面允许跟冒号（中英文）、破折号或空白。
COLON_TITLE_RE = re.compile(
    r"(?:订阅成功|添加订阅成功|已添加订阅|新增订阅|订阅已创建|已创建订阅|"
    r"删除订阅|已删除订阅|取消订阅|移除订阅|订阅已删除)"
    r"\s*[：:\-—]\s*(.+)"
)
# 常见标题字段名，用于从 context 字典里直接取值。
TITLE_KEYS = ("title", "name", "media_title", "title_year", "show_name", "subject")
# 常见媒体类型字段名。
TYPE_KEYS = ("type", "media_type", "mtype")
# 常见 TMDB 字段名。
TMDB_KEYS = ("tmdb_id", "tmdbid", "tmdb")
# 常见年份字段名。
YEAR_KEYS = ("year", "release_year", "first_air_date")


class MediaAutomatorSync(_PluginBase):
    """把 Media Automator 的订阅变更镜像到 MoviePilot。"""

    # 插件名称
    plugin_name = "Media Automator 订阅同步"
    # 插件描述
    plugin_desc = "读取 Media Automator 日志（支持远程HTTP或本地文件），自动同步订阅的新增与删除。"
    # 插件图标
    plugin_icon = "MediaAutomator_A.png"
    # 插件版本
    plugin_version = "1.2.3"
    # 插件作者
    plugin_author = "PeACE2323"
    # 作者主页
    author_url = "https://github.com/PeACE2323"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediaautomatorsync_"
    # 加载顺序
    plugin_order = 60
    # 可使用的用户级别
    auth_level = 1

    # 进程内保护状态读写的锁，日志轮询与手动 API 可能并发触发。
    _state_lock = Lock()

    def __init__(self) -> None:
        """初始化插件并声明本次运行的状态字段。"""
        super().__init__()
        # 配置项
        self._enabled: bool = False
        self._notify: bool = False
        self._onlyonce: bool = False
        self._log_source: str = LOG_SOURCE_FILE
        self._log_path: str = DEFAULT_LOG_PATH
        self._ma_url: str = ""
        self._ma_username: str = ""
        self._ma_password: str = ""
        self._ma_token: str = ""
        self._ma_login_path: str = ""
        self._ma_timeout: int = 30
        self._ma_verify_ssl: bool = True
        self._cron: str = "*/1 * * * *"
        self._add_patterns: str = DEFAULT_ADD_PATTERNS
        self._delete_patterns: str = DEFAULT_DELETE_PATTERNS
        self._sync_add: bool = True
        self._sync_delete: bool = True
        self._default_type: str = "auto"
        self._max_lines: int = 2000
        self._max_age_days: int = 30
        self._history_limit: int = 200
        # 运行时状态
        self._cursor: Dict[str, Any] = {}
        self._http_state: Dict[str, Any] = {}
        self._history: List[Dict[str, Any]] = []
        self._last_error: str = ""
        self._last_run: str = ""
        # 最近一次手动操作的结果，供详情页在重新加载后展示。
        self._last_action: Dict[str, Any] = {}
        # 远程客户端按配置缓存，登录得到的 Token 可以跨轮询复用。
        self._client: Optional[MediaAutomatorClient] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        生效配置信息。

        必须允许重复调用：宿主在保存配置、更新插件和创建分身时都会重新执行，
        这里每次都从插件存储里重建状态，而不是在旧状态上累加。
        """
        config = config or {}
        # 先读取配置，缺省时保留类默认值，避免旧配置升级后字段缺失导致状态归零。
        self._enabled = bool(config.get("enabled", self._enabled))
        self._notify = bool(config.get("notify", self._notify))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._log_source = str(config.get("log_source") or LOG_SOURCE_FILE).strip().lower()
        if self._log_source not in (LOG_SOURCE_FILE, LOG_SOURCE_HTTP):
            self._log_source = LOG_SOURCE_FILE
        self._log_path = str(config.get("log_path") or DEFAULT_LOG_PATH).strip()
        # 远程模式配置
        self._ma_url = str(config.get("ma_url") or "").strip()
        self._ma_username = str(config.get("ma_username") or "root").strip()
        self._ma_password = str(config.get("ma_password") or "")
        self._ma_token = str(config.get("ma_token") or "").strip()
        self._ma_login_path = str(config.get("ma_login_path") or "").strip()
        self._ma_timeout = self._parse_positive_int(config.get("ma_timeout"), 30)
        self._ma_verify_ssl = bool(config.get("ma_verify_ssl", True))
        self._cron = str(config.get("cron") or self._cron).strip()
        self._add_patterns = str(config.get("add_patterns") or DEFAULT_ADD_PATTERNS)
        self._delete_patterns = str(config.get("delete_patterns") or DEFAULT_DELETE_PATTERNS)
        self._sync_add = bool(config.get("sync_add", True))
        self._sync_delete = bool(config.get("sync_delete", True))
        self._default_type = str(config.get("default_type") or "auto")
        self._max_lines = self._parse_positive_int(config.get("max_lines"), 2000)
        self._max_age_days = self._parse_positive_int(config.get("max_age_days"), 30)
        self._history_limit = self._parse_positive_int(config.get("history_limit"), 200)

        # 读取持久化的读取位置、行指纹与处理历史。
        self._cursor = self.get_data("cursor") or {}
        self._http_state = self.get_data("http_state") or {}
        self._history = self.get_data("history") or []
        self._last_action = self.get_data("last_action") or {}
        if not isinstance(self._cursor, dict):
            self._cursor = {}
        if not isinstance(self._http_state, dict):
            self._http_state = {}
        if not isinstance(self._history, list):
            self._history = []
        if not isinstance(self._last_action, dict):
            self._last_action = {}

        # 每次初始化重建客户端：配置可能已变，旧的 Token 不应被沿用。
        self._client = None

        # 一次性开关处理后立即回写，防止宿主重载时反复触发。
        if self._onlyonce:
            self.sync()
            self._onlyonce = False
            self.__update_config()

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令，便于在消息渠道里手动触发一次同步。"""
        from app.schemas.types import EventType

        return [
            {
                "cmd": "/media_automator_sync",
                "event": EventType.PluginAction,
                "desc": "同步 Media Automator 订阅",
                "category": "订阅",
                "data": {"action": "media_automator_sync"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件 API，供前端手动触发与查看运行状态。

        详情页的按钮走宿主统一请求客户端，它按标准三段式 ``Response`` 解析响应；
        因此这里显式声明 ``response_model``，端点也统一返回 ``schemas.Response``。
        返回裸字典会让前端判定为「服务器返回了无效响应」。
        """
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即执行一次订阅同步",
                "response_model": schemas.Response,
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询插件运行状态",
                "response_model": schemas.Response,
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "测试日志来源连通性",
                "response_model": schemas.Response,
            },
            {
                "path": "/reconcile",
                "endpoint": self.api_reconcile,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查缺补漏：补齐 MoviePilot 缺少的订阅",
                "response_model": schemas.Response,
            },
            {
                "path": "/probe",
                "endpoint": self.api_probe,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "预览日志解析结果",
                "response_model": schemas.Response,
            },
            {
                "path": "/reset",
                "endpoint": self.api_reset,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空读取位置与同步历史",
                "response_model": schemas.Response,
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期任务，按配置的 cron 轮询日志。"""
        if not self._enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError as error:
            logger.error(f"Media Automator 订阅同步：cron 表达式无效 {self._cron}：{error}")
            return []
        return [
            {
                "id": "MediaAutomatorSync",
                "name": "Media Automator 订阅同步服务",
                "trigger": trigger,
                "func": self.sync,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页面与默认配置模型。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "log_source",
                                            "label": "日志来源",
                                            "items": [
                                                {"title": "远程 HTTP（填地址与账号密码）", "value": "http"},
                                                {"title": "本地文件（需挂载日志目录）", "value": "file"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "日志轮询周期",
                                            "placeholder": "5位cron表达式",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 远程 HTTP 模式 ----
                    {
                        "component": "VRow",
                        "props": {"show": "log_source === 'http'"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_url",
                                            "label": "Media Automator 地址",
                                            "placeholder": "http://192.168.6.2:8010",
                                            "hint": "含协议与端口，末尾不要带 /",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_username",
                                            "label": "用户名",
                                            "placeholder": "root",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_password",
                                            "label": "密码",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"show": "log_source === 'http'"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_token",
                                            "label": "Token（可选，留空则用账号密码登录）",
                                            "hint": "填了 Token 就直接用它，不再登录；Token 过期会自动回退到账号密码",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_login_path",
                                            "label": "登录接口路径（可选）",
                                            "placeholder": "留空自动探测",
                                            "hint": "自动探测失败时，用浏览器 F12 抓登录请求的路径填这里",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ma_timeout",
                                            "label": "超时(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 本地文件模式 ----
                    {
                        "component": "VRow",
                        "props": {"show": "log_source === 'file'"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "log_path",
                                            "label": "Media Automator 日志文件路径",
                                            "placeholder": DEFAULT_LOG_PATH,
                                            "hint": "填容器内可读的 app.log 完整路径，需要先把日志目录以只读方式挂载进来",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "sync_add", "label": "同步新增订阅"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "sync_delete", "label": "同步删除订阅"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "default_type",
                                            "label": "无法判断类型时",
                                            "items": [
                                                {"title": "按标题自动识别", "value": "auto"},
                                                {"title": "当作电影", "value": "movie"},
                                                {"title": "当作电视剧", "value": "tv"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "add_patterns",
                                            "label": "新增订阅匹配正则",
                                            "rows": 2,
                                            "placeholder": DEFAULT_ADD_PATTERNS,
                                            "hint": "在日志 message 中命中即视为新增订阅，多个关键词用 | 分隔",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "delete_patterns",
                                            "label": "删除订阅匹配正则",
                                            "rows": 2,
                                            "placeholder": DEFAULT_DELETE_PATTERNS,
                                            "hint": "在日志 message 中命中即视为删除订阅，多个关键词用 | 分隔",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_lines",
                                            "label": "单次最多处理行数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_age_days",
                                            "label": "只处理最近N天的日志",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "history_limit",
                                            "label": "保留历史记录条数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "远程 HTTP 模式：填 Media Automator 地址与账号密码即可，无需挂载。"
                                                "插件会登录后调用 /api/system/logs/download 拉取日志。"
                                                "登录接口留空时自动探测（先读 openapi.json，再试常见路径）；"
                                                "若探测失败，用浏览器 F12 抓登录请求的路径填进去。\n"
                                                "建议轮询周期设为 2~5 分钟：该接口每次返回整份日志文件。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "log_source": LOG_SOURCE_HTTP,
            "log_path": DEFAULT_LOG_PATH,
            "ma_url": "",
            "ma_username": "root",
            "ma_password": "",
            "ma_token": "",
            "ma_login_path": "",
            "ma_timeout": 30,
            "ma_verify_ssl": True,
            "cron": "*/2 * * * *",
            "sync_add": True,
            "sync_delete": True,
            "default_type": "auto",
            "add_patterns": DEFAULT_ADD_PATTERNS,
            "delete_patterns": DEFAULT_DELETE_PATTERNS,
            "max_lines": 2000,
            "max_age_days": 30,
            "history_limit": 200,
        }

    def get_page(self) -> List[dict]:
        """返回详情页，展示操作按钮、运行状态与处理历史。"""
        contents: List[dict] = []

        # 操作按钮区：详情页不能输入，因此用带固定参数的 API 调用实现「点一下就跑」。
        contents.append(
            {
                "component": "VRow",
                "props": {"class": "mb-2"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "flat",
                                    "block": True,
                                    "prepend-icon": "mdi-sync",
                                },
                                "text": "立即同步一次",
                                "events": {
                                    "click": {
                                        "api": "plugin/MediaAutomatorSync/run",
                                        "method": "get",
                                    }
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "info",
                                    "variant": "tonal",
                                    "block": True,
                                    "prepend-icon": "mdi-connection",
                                },
                                "text": "测试连接",
                                "events": {
                                    "click": {
                                        "api": "plugin/MediaAutomatorSync/test",
                                        "method": "get",
                                    }
                                },
                            }
                        ],
                    },
                ],
            }
        )

        # 查缺补漏：需要远程 HTTP 模式才能读取 MA 的订阅清单。
        if self._log_source == LOG_SOURCE_HTTP:
            contents.append(
                {
                    "component": "VRow",
                    "props": {"class": "mb-2"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "warning",
                                        "variant": "tonal",
                                        "block": True,
                                        "prepend-icon": "mdi-clipboard-search-outline",
                                    },
                                    "text": "查缺补漏（预演）",
                                    "events": {
                                        "click": {
                                            "api": "plugin/MediaAutomatorSync/reconcile",
                                            "method": "get",
                                            "params": {"dry_run": True},
                                        }
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "success",
                                        "variant": "flat",
                                        "block": True,
                                        "prepend-icon": "mdi-playlist-plus",
                                    },
                                    "text": "查缺补漏（补齐订阅）",
                                    "events": {
                                        "click": {
                                            "api": "plugin/MediaAutomatorSync/reconcile",
                                            "method": "get",
                                        }
                                    },
                                }
                            ],
                        },
                    ],
                }
            )
            contents.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mb-2",
                        "text": (
                            "「预演」只列出 MoviePilot 缺少的订阅，不会写入；"
                            "「补齐」会真正添加。也可以直接在地址栏调用 "
                            "/reconcile?limit=5 分批补，避免一次添加过多。"
                        ),
                    },
                }
            )

        # 最近一次手动操作的结果。
        # 详情页按钮的机制是「调接口 → 重新加载页面数据」，接口返回值不会被渲染，
        # 所以这里把结果读出来展示，否则用户点击后只会看到进度条闪一下就消失。
        if self._last_action:
            detail_text = self._last_action.get("message") or ""
            details = self._last_action.get("details") or []
            if details:
                detail_text += "\n" + "\n".join(f"· {line}" for line in details)
            contents.append(
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": "primary", "class": "mb-2"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {"class": "text-subtitle-1"},
                            "text": (
                                f"{self._last_action.get('title') or '操作结果'}"
                                f"（{self._last_action.get('time') or ''}）"
                            ),
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "text-body-2", "style": "white-space: pre-wrap;"},
                            "text": detail_text,
                        },
                    ],
                }
            )

        # 运行状态摘要
        if self._log_source == LOG_SOURCE_HTTP:
            source_line = f"日志来源：远程 HTTP（{self._ma_url or '未填写地址'}，用户 {self._ma_username}）"
            if self._http_state.get("last_count"):
                progress_line = f"上次读取到：{self._http_state.get('last_count')} 行"
            else:
                progress_line = "读取位置：尚未拉取"
        else:
            source_line = f"日志来源：本地文件（{self._log_path}）"
            progress_line = f"读取位置：{self._cursor.get('offset', 0)} 字节"

        status_lines = [
            f"启用状态：{'已启用' if self._enabled else '未启用'}",
            source_line,
            f"轮询周期：{self._cron}",
            f"上次执行：{self._last_run or '尚未执行'}",
            progress_line,
        ]
        if self._last_error:
            status_lines.append(f"最近错误：{self._last_error}")
        contents.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "warning" if self._last_error else "info",
                    "variant": "tonal",
                    "text": "\n".join(status_lines),
                },
            }
        )

        if not self._history:
            contents.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无同步记录。可点击上方「立即同步一次」，或用「查缺补漏」补齐历史订阅。",
                    },
                }
            )
            return contents

        # 历史记录卡片，按时间倒序展示
        for item in sorted(self._history, key=lambda x: x.get("time") or "", reverse=True)[: self._history_limit]:
            action = item.get("action")
            if action == "add":
                action_text, color = "新增订阅", "success"
            elif action == "delete":
                action_text, color = "删除订阅", "error"
            else:
                action_text, color = "已跳过", "grey"

            contents.append(
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "pa-2"},
                            "text": (
                                f"[{action_text}] {item.get('title') or '未知标题'}"
                                f"（{item.get('year') or '未知年份'}）"
                            ),
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "pa-2 text-caption"},
                            "text": (
                                f"媒体来源：{item.get('media_source') or '-'} / "
                                f"{item.get('media_id') or '-'}　"
                                f"结果：{item.get('message') or '-'}　"
                                f"时间：{item.get('time') or '-'}"
                            ),
                        },
                    ],
                }
            )
        return contents

    def stop_service(self) -> None:
        """释放插件资源。本插件不持有线程或连接，仅把状态落盘。"""
        # 周期任务由宿主调度器管理，这里不需要手动撤销；
        # 把读取位置与历史刷入插件存储，避免进程退出丢失进度。
        try:
            self.__persist_state()
        except Exception as error:  # 停止过程不应因写盘失败而抛出
            logger.error(f"Media Automator 订阅同步：退出时保存状态失败：{error}")

    # ------------------------------------------------------------------
    # 插件 API
    # ------------------------------------------------------------------

    def api_run(self) -> schemas.Response:
        """立即执行一次同步并返回本次处理结果。"""
        result = self.sync()
        # sync() 返回的是普通统计字典，这里包装成宿主统一的响应 envelope。
        return schemas.Response(
            success=bool(result.get("success")),
            message=str(result.get("message") or ""),
            data=result,
        )

    def api_status(self) -> schemas.Response:
        """返回插件当前运行状态。"""
        status = {
            "enabled": self._enabled,
            "log_source": self._log_source,
            "cron": self._cron,
            "last_run": self._last_run,
            "last_error": self._last_error,
            "history_count": len(self._history),
        }
        if self._log_source == LOG_SOURCE_HTTP:
            status.update(
                {
                    "ma_url": self._ma_url,
                    "ma_username": self._ma_username,
                    "has_token": bool(self._ma_token),
                    "login_path": self._ma_login_path or "自动探测",
                    "http_state": self._http_state,
                }
            )
        else:
            status.update(
                {
                    "log_path": self._log_path,
                    "log_exists": Path(self._log_path).exists() if self._log_path else False,
                    "cursor": self._cursor,
                }
            )
        return schemas.Response(success=True, message="", data=status)

    def __record_action(self, title: str, message: str, details: Optional[List[str]] = None) -> None:
        """
        记录一次手动操作的结果，供详情页展示。

        详情页按钮的机制是「调接口 → 重新加载页面数据」，接口的返回值不会被渲染；
        因此结果必须写进插件数据，页面重新加载后才能看到，否则点击只会「闪一下」。
        """
        entry = {
            "title": title,
            "message": message,
            "details": (details or [])[:50],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self._state_lock:
            self._last_action = entry
            self.save_data("last_action", entry)

    def api_test(self) -> schemas.Response:
        """
        测试与 Media Automator 的连通性与登录。

        HTTP 模式下会真实登录并尝试读取日志，用于在配置页快速验证账号密码是否正确；
        文件模式下检查日志文件是否可读。
        """
        if self._log_source != LOG_SOURCE_HTTP:
            path = Path(self._log_path) if self._log_path else None
            if not path or not path.exists():
                message = f"日志文件不存在：{self._log_path}"
                self.__record_action("测试连接", message)
                return schemas.Response(success=False, message=message)
            try:
                lines = self.__read_tail_lines(self._log_path, 1)
            except (OSError, ValueError) as error:
                message = f"读取日志失败：{error}"
                self.__record_action("测试连接", message)
                return schemas.Response(success=False, message=message)
            message = f"日志文件可读，大小 {path.stat().st_size} 字节，末行可得 {len(lines)} 行"
            self.__record_action("测试连接", message)
            return schemas.Response(success=True, message=message)

        client = self.__get_client()
        ok, message = self.__run_async(client.login_only())
        # 登录成功即把探测到的登录路径回写，减少后续轮询的探测开销。
        if ok and client._resolved_login_path and not self._ma_login_path:
            self._ma_login_path = client._resolved_login_path
            self.__update_config()
        self.__record_action("测试连接", message)
        return schemas.Response(success=ok, message=message)

    def api_probe(self, limit: int = 50) -> schemas.Response:
        """预览日志解析结果，用于核对正则是否与用户的 Media Automator 版本匹配。"""
        limit = self._parse_positive_int(limit, 50)

        # 远程模式下拉取日志；失败时返回明确错误而不是空结果。
        if self._log_source == LOG_SOURCE_HTTP:
            client = self.__get_client()
            text = self.__run_async(client.fetch_log())
            if text is None:
                return schemas.Response(
                    success=False,
                    message=f"拉取远程日志失败：{client.last_error}",
                    data=[],
                )
            lines = [line for line in text.splitlines() if line.strip()][-limit:]
        else:
            try:
                lines = self.__read_tail_lines(self._log_path, limit)
            except OSError as error:
                return schemas.Response(success=False, message=f"读取日志失败：{error}", data=[])
            except ValueError as error:
                return schemas.Response(success=False, message=str(error), data=[])

        parsed: List[Dict[str, Any]] = []
        for raw in lines:
            record = self.__parse_log_line(raw)
            if record is None:
                continue
            action = self.__classify(record)
            parsed.append(
                {
                    "timestamp": record.get("timestamp"),
                    "level": record.get("level"),
                    "module": record.get("log_module"),
                    "message": (record.get("message") or "")[:300],
                    "event_type": record.get("event_type"),
                    "detected_action": action or "",
                    "media": self.__extract_media(record) if action else None,
                }
            )
        return schemas.Response(success=True, message=f"已解析 {len(parsed)} 行", data=parsed)

    def api_reconcile(self, limit: int = 0, dry_run: bool = False) -> schemas.Response:
        """
        手动执行一次「查缺补漏」：把 Media Automator 的全部订阅与 MoviePilot 比对，
        只补齐 MoviePilot 缺少的那些。

        :param limit: 单次最多补几条；0 表示不限制
        :param dry_run: 为真时只列出将要补的条目，不实际写入
        :return: 统计结果
        """
        if self._log_source != LOG_SOURCE_HTTP:
            message = "查缺补漏需要「远程 HTTP」日志来源（要调用订阅清单接口）"
            self.__record_action("查缺补漏", message)
            return schemas.Response(success=False, message=message)
        if not self._enabled:
            self.__record_action("查缺补漏", "插件未启用")
            return schemas.Response(success=False, message="插件未启用")

        limit = self._parse_non_negative_int(limit, 0)
        client = self.__get_client()
        records = self.__run_async(client.fetch_subscriptions())
        if records is None:
            self._last_error = client.last_error or "拉取订阅清单失败"
            self.__record_action("查缺补漏", f"失败：{self._last_error}")
            return schemas.Response(success=False, message=self._last_error, data=[])

        existing = 0
        added = 0
        failed = 0
        planned: List[Dict[str, Any]] = []
        details: List[Dict[str, Any]] = []

        for item in records:
            tmdb_id = item["tmdb_id"]
            # 先按 TMDB 身份判断 MoviePilot 是否已有，避免重复添加。
            try:
                if SubscribeOper().exists(media_source=MediaSource.TMDB, media_id=tmdb_id):
                    existing += 1
                    continue
            except Exception as error:
                logger.warning(f"Media Automator 查缺补漏：查询既有订阅失败：{error}")

            if limit and added + len(planned) >= limit:
                # 达到本次上限，剩余条目留给下次。
                break

            if dry_run:
                planned.append(item)
                continue

            media = {
                "title": item["title"],
                "year": item["year"],
                "media_id": tmdb_id,
                "media_source": MediaSource.TMDB.value,
                "raw_type": item["media_type"],
            }
            try:
                outcome = self.__apply_add(media, {})
            except Exception as error:  # 单条失败不影响其余条目
                logger.error(
                    f"Media Automator 查缺补漏：补订「{item['title']}」失败：{error}",
                    exc_info=True,
                )
                failed += 1
                details.append({**item, "result": f"失败：{error}"})
                continue
            if outcome.get("changed"):
                added += 1
                self.__record_history("add", media, f"查缺补漏：{outcome.get('message')}")
            else:
                existing += 1
            details.append({**item, "result": outcome.get("message")})

        summary = (
            f"MA 共 {len(records)} 条订阅，MP 已有 {existing} 条，"
            f"{'待补' if dry_run else '新补'} {len(planned) if dry_run else added} 条，"
            f"失败 {failed} 条"
        )
        logger.info(f"Media Automator 查缺补漏完成：{summary}")
        if not dry_run and (added or failed):
            self.__notify_reconcile(summary)
        # 详情页不渲染接口返回值，把结果落盘供页面重新加载后展示。
        detail_lines = [
            f"{item['title']}（{item['year'] or '未知年份'}）"
            + (f" → {item.get('result')}" if not dry_run else "")
            for item in (planned if dry_run else details)
        ]
        self.__record_action(
            "查缺补漏（预演）" if dry_run else "查缺补漏（补齐订阅）",
            summary,
            detail_lines,
        )
        return schemas.Response(
            success=True,
            message=summary,
            data=planned if dry_run else details,
        )

    def api_reset(self) -> schemas.Response:
        """清空读取位置、远程行指纹与同步历史。"""
        with self._state_lock:
            self._cursor = {}
            self._http_state = {}
            self._history = []
            self._last_error = ""
            self.__persist_state()
        return schemas.Response(success=True, message="已清空读取位置与同步历史")

    # ------------------------------------------------------------------
    # 核心同步逻辑
    # ------------------------------------------------------------------

    def sync(self) -> Dict[str, Any]:
        """
        取得日志增量并同步订阅变更。

        这是宿主调度器与远程命令调用的同步入口。HTTP 模式内部需要异步请求，
        这里用一个临时事件循环驱动，避免让宿主调度线程去 await。
        """
        if self._log_source == LOG_SOURCE_HTTP:
            return self.__run_async(self.__sync_http())
        return self.__sync_from_lines()

    @staticmethod
    def __run_async(coro: Any) -> Any:
        """
        在同步上下文中执行协程。

        宿主调度器以同步方式调用插件服务，因此这里自建一个事件循环跑完即关，
        不长期占用线程或 loop。
        """
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 当前线程没有运行中的 loop，自建一个。
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
        # 已经处于事件循环中（例如被异步 API 调用）：交给新线程执行。
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()

    def __get_client(self) -> MediaAutomatorClient:
        """按当前配置返回（并缓存）远程客户端，使登录 Token 可跨轮询复用。"""
        if self._client is None:
            self._client = MediaAutomatorClient(
                base_url=self._ma_url,
                username=self._ma_username,
                password=self._ma_password,
                login_path=self._ma_login_path,
                token=self._ma_token,
                timeout=self._ma_timeout,
                verify_ssl=self._ma_verify_ssl,
            )
        return self._client

    async def __sync_http(self) -> Dict[str, Any]:
        """HTTP 模式：拉取日志全文，按行指纹去重后同步。"""
        started = datetime.now()
        self._last_run = started.strftime("%Y-%m-%d %H:%M:%S")
        result: Dict[str, Any] = {
            "success": True,
            "message": "",
            "scanned": 0,
            "added": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
        }

        if not self._enabled:
            result["success"] = False
            result["message"] = "插件未启用"
            return result

        client = self.__get_client()
        text = await client.fetch_log()
        if text is None:
            self._last_error = client.last_error or "拉取远程日志失败"
            result["success"] = False
            result["message"] = self._last_error
            logger.warning(f"Media Automator 订阅同步：{self._last_error}")
            self.__persist_state()
            return result

        self._last_error = ""
        # 下载端点返回整份文件，因此这里做行级去重而不是字节偏移。
        lines, new_state = self.__diff_remote_lines(text)
        result["scanned"] = len(lines)

        self.__process_lines(lines, result)

        self._http_state = new_state
        self.__persist_state()

        result["message"] = self.__format_result(result)
        logger.info(f"Media Automator 订阅同步完成：{result['message']}")
        if self._notify and (result["added"] or result["deleted"] or result["failed"]):
            self.__notify_result(result)
        return result

    def __diff_remote_lines(self, text: str) -> Tuple[List[str], Dict[str, Any]]:
        """
        从整份日志文本中挑出尚未处理过的行。

        接口每次返回全部内容，所以用「上一批最后一行的指纹 + 行数」定位新增区段：
        正常情况下新内容是追加的，直接取旧行数之后的部分；若日志被轮转或清理导致
        行数减少、或末尾指纹不匹配，则退化为只保留最近 ``max_lines`` 行。
        """
        all_lines = [line for line in text.splitlines() if line.strip()]
        last_fingerprint = str(self._http_state.get("last_fingerprint") or "")
        last_count = int(self._http_state.get("last_count") or 0)

        new_lines: List[str] = []
        if last_count and last_count <= len(all_lines):
            # 校验旧批次的末尾是否仍然对得上，对得上才是纯追加。
            if not last_fingerprint or self.__line_fingerprint(all_lines[last_count - 1]) == last_fingerprint:
                new_lines = all_lines[last_count:]
            else:
                logger.info("Media Automator 远程日志：末尾指纹不匹配（可能被轮转），按最近行处理")
                new_lines = all_lines[-self._max_lines:]
        elif last_count:
            # 行数变少说明日志被轮转或清理了。
            logger.info("Media Automator 远程日志：行数减少（日志已轮转），按最近行处理")
            new_lines = all_lines[-self._max_lines:]
        else:
            # 首次运行：只取最近若干行，避免把历史订阅一次性灌进 MoviePilot。
            new_lines = all_lines[-self._max_lines:]

        # 单次处理上限，防止一次导入过多。
        if len(new_lines) > self._max_lines:
            new_lines = new_lines[-self._max_lines:]

        state = {
            "last_count": len(all_lines),
            "last_fingerprint": self.__line_fingerprint(all_lines[-1]) if all_lines else "",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return new_lines, state

    @staticmethod
    def __line_fingerprint(line: str) -> str:
        """计算一行日志的指纹，用于判断批次边界。"""
        import hashlib

        return hashlib.sha1(line.encode("utf-8", errors="replace")).hexdigest()

    def __sync_from_lines(self) -> Dict[str, Any]:
        """文件模式：增量读取本地日志文件并同步。"""
        started = datetime.now()
        self._last_run = started.strftime("%Y-%m-%d %H:%M:%S")
        result: Dict[str, Any] = {
            "success": True,
            "message": "",
            "scanned": 0,
            "added": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
        }

        # 未启用时直接返回，避免手动 API 绕过开关执行同步。
        if not self._enabled:
            result["success"] = False
            result["message"] = "插件未启用"
            return result

        try:
            lines, new_cursor = self.__read_new_lines()
        except FileNotFoundError:
            self._last_error = f"日志文件不存在：{self._log_path}"
            result["success"] = False
            result["message"] = self._last_error
            logger.warning(f"Media Automator 订阅同步：{self._last_error}")
            self.__persist_state()
            return result
        except (OSError, ValueError) as error:
            self._last_error = f"读取日志失败：{error}"
            result["success"] = False
            result["message"] = self._last_error
            logger.error(f"Media Automator 订阅同步：{self._last_error}")
            self.__persist_state()
            return result

        self._last_error = ""
        result["scanned"] = len(lines)

        self.__process_lines(lines, result)

        # 处理完成后推进读取位置并落盘。
        with self._state_lock:
            self._cursor = new_cursor
            self.__persist_state()

        result["message"] = self.__format_result(result)
        logger.info(f"Media Automator 订阅同步完成：{result['message']}")

        if self._notify and (result["added"] or result["deleted"] or result["failed"]):
            self.__notify_result(result)
        return result

    @staticmethod
    def __format_result(result: Dict[str, Any]) -> str:
        """把统计结果格式化为一行说明。"""
        return (
            f"扫描 {result['scanned']} 行，新增订阅 {result['added']} 条，"
            f"删除订阅 {result['deleted']} 条，跳过 {result['skipped']} 条，"
            f"失败 {result['failed']} 条"
        )

    def __process_lines(self, lines: List[str], result: Dict[str, Any]) -> None:
        """逐行解析并执行订阅同步，就地更新统计。"""
        # 按时间顺序处理，保证新增先于删除的因果顺序不被破坏。
        for raw in lines:
            record = self.__parse_log_line(raw)
            if record is None:
                continue
            action = self.__classify(record)
            if not action:
                continue
            if action == "add" and not self._sync_add:
                result["skipped"] += 1
                continue
            if action == "delete" and not self._sync_delete:
                result["skipped"] += 1
                continue

            media = self.__extract_media(record)
            if not media.get("title") and not media.get("media_id"):
                result["skipped"] += 1
                continue

            try:
                if action == "add":
                    outcome = self.__apply_add(media, record)
                else:
                    outcome = self.__apply_delete(media, record)
            except Exception as error:  # 单条失败不能中断整批同步
                logger.error(
                    f"Media Automator 订阅同步：处理「{media.get('title')}」失败：{error}",
                    exc_info=True,
                )
                result["failed"] += 1
                self.__record_history(action, media, f"处理失败：{error}")
                continue

            if outcome.get("changed"):
                if action == "add":
                    result["added"] += 1
                else:
                    result["deleted"] += 1
            else:
                result["skipped"] += 1
            self.__record_history(action, media, outcome.get("message") or "")

    def __apply_add(self, media: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        """
        在 MoviePilot 上添加订阅。

        :param media: 从日志中提取的媒体信息
        :param record: 原始日志记录
        :return: 含 changed 与 message 的结果字典
        """
        title = media.get("title") or ""
        year = str(media.get("year") or "")
        media_id = media.get("media_id")
        mtype = self.__resolve_media_type(media, record)

        # 没有标题也没有 TMDB ID 时无法识别，直接跳过。
        if not title and not media_id:
            return {"changed": False, "message": "日志中未提取到标题或 TMDB ID"}

        # 判断订阅是否已存在，避免重复添加。
        if media_id:
            try:
                if SubscribeOper().exists(
                    media_source=MediaSource.TMDB,
                    media_id=str(media_id),
                ):
                    return {"changed": False, "message": "MoviePilot 上已存在该订阅"}
            except Exception as error:
                logger.warning(f"Media Automator 订阅同步：查询既有订阅失败：{error}")

        # 优先使用 TMDB ID 订阅，命中率高于按标题识别。
        # SubscribeChain.add 失败时返回 (None, 原因)，不是 0。
        sid, message = SubscribeChain().add(
            title=title,
            year=year,
            mtype=mtype,
            media_source=MediaSource.TMDB if media_id else None,
            media_id=str(media_id) if media_id else None,
            username="Media Automator",
            exist_ok=True,
        )
        # TMDB 身份识别失败时回退到按标题识别，尽量不丢订阅。
        if not sid and media_id and title:
            logger.warning(
                f"Media Automator 订阅同步：TMDB {media_id} 订阅失败（{message}），回退按标题识别：{title}"
            )
            sid, message = SubscribeChain().add(
                title=title,
                year=year,
                mtype=mtype,
                username="Media Automator",
                exist_ok=True,
            )
        if not sid:
            return {"changed": False, "message": f"添加订阅失败：{message}"}
        return {"changed": True, "message": f"已添加订阅（{message}）"}

    def __apply_delete(self, media: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        """
        在 MoviePilot 上删除订阅。

        :param media: 从日志中提取的媒体信息
        :param record: 原始日志记录
        :return: 含 changed 与 message 的结果字典
        """
        oper = SubscribeOper()
        candidates: List[Any] = []
        media_id = media.get("media_id")

        # 优先按 TMDB 身份精确匹配。
        if media_id:
            try:
                candidates = oper.list_by_media_identity(
                    media_source=MediaSource.TMDB,
                    media_id=str(media_id),
                ) or []
            except Exception as error:
                logger.warning(f"Media Automator 订阅同步：按 TMDB 身份查询订阅失败：{error}")
                candidates = []

        # 退化到按标题匹配，与 Media Automator「删除标题包含关键词的订阅」语义一致。
        title = media.get("title")
        if not candidates and title:
            try:
                all_subs = oper.list() or []
            except Exception as error:
                logger.warning(f"Media Automator 订阅同步：读取订阅列表失败：{error}")
                all_subs = []
            keyword = str(title).strip()
            candidates = [
                sub for sub in all_subs
                if keyword and keyword in str(getattr(sub, "name", "") or "")
            ]

        if not candidates:
            return {"changed": False, "message": "MoviePilot 上未找到匹配的订阅"}

        deleted = 0
        for sub in candidates:
            sid = getattr(sub, "id", None)
            if not sid:
                continue
            try:
                oper.delete(sid)
                deleted += 1
                logger.info(f"Media Automator 订阅同步：已删除订阅 {sid}（{getattr(sub, 'name', '')}）")
            except Exception as error:
                logger.error(f"Media Automator 订阅同步：删除订阅 {sid} 失败：{error}")
        if not deleted:
            return {"changed": False, "message": "删除订阅失败"}
        return {"changed": True, "message": f"已删除 {deleted} 条订阅"}

    # ------------------------------------------------------------------
    # 日志读取与解析
    # ------------------------------------------------------------------

    def __read_new_lines(self) -> Tuple[List[str], Dict[str, Any]]:
        """
        从上次位置增量读取日志行。

        :return: (新增的文本行, 新的游标)
        :raises FileNotFoundError: 日志文件不存在
        :raises OSError: 文件读取失败
        :raises ValueError: 配置无效
        """
        path = Path(self._log_path)
        if not self._log_path:
            raise ValueError("未配置日志文件路径")
        if not path.exists():
            raise FileNotFoundError(self._log_path)
        if path.is_dir():
            raise ValueError(f"日志路径是目录而不是文件：{self._log_path}")

        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        offset = int(self._cursor.get("offset") or 0)
        stored_identity = self._cursor.get("identity")

        # 文件被轮转或替换时 inode/size 会变化，此时从头读取新文件。
        if stored_identity and stored_identity != identity:
            logger.info("Media Automator 订阅同步：检测到日志轮转，从新文件开头读取")
            offset = 0
        if offset > stat.st_size:
            logger.info("Media Automator 订阅同步：日志文件变小，判定为轮转，重新从头读取")
            offset = 0

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            raw_lines = handle.readlines()
            new_offset = handle.tell()

        # 限制单次处理量，避免一次导入巨量历史日志把宿主拖慢。
        truncated = False
        if len(raw_lines) > self._max_lines:
            raw_lines = raw_lines[-self._max_lines:]
            truncated = True

        lines = [line.rstrip("\n") for line in raw_lines if line.strip()]
        cursor = {
            "identity": identity,
            "offset": new_offset,
            "size": stat.st_size,
            "truncated": truncated,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return lines, cursor

    @staticmethod
    def __read_tail_lines(log_path: str, limit: int) -> List[str]:
        """读取日志文件末尾若干行，用于解析预览。"""
        if not log_path:
            raise ValueError("未配置日志文件路径")
        path = Path(log_path)
        if not path.exists():
            raise FileNotFoundError(log_path)
        if path.is_dir():
            raise ValueError(f"日志路径是目录而不是文件：{log_path}")
        # 只读取末尾 2MB，避免大日志文件把内存占满。
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 2 * 1024 * 1024))
            chunk = handle.read()
        text = chunk.decode("utf-8", errors="replace")
        return [line for line in text.splitlines() if line.strip()][-limit:]

    def __parse_log_line(self, raw: str) -> Optional[Dict[str, Any]]:
        """
        把一行日志解析成结构化记录。

        Media Automator 写的是 JSON 行；若遇到非 JSON 行（例如启动早期的纯文本日志），
        退化为把整行当作 message 处理，保证老版本或异常行仍有机会被识别。
        """
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"message": text, "raw": text}
        if not isinstance(parsed, dict):
            return {"message": text, "raw": text}
        parsed.setdefault("raw", text)
        return parsed

    def __classify(self, record: Dict[str, Any]) -> Optional[str]:
        """
        判断日志记录属于新增还是删除事件。

        :return: "add" / "delete"，未命中返回 None
        """
        # 时间过滤：只处理最近 N 天的事件，避免首次运行导入陈旧日志。
        timestamp = self.__parse_timestamp(record.get("timestamp"))
        if timestamp and self._max_age_days > 0:
            if timestamp < datetime.now() - timedelta(days=self._max_age_days):
                return None

        # 优先使用 event_type 判断，它比人类可读文案稳定得多。
        event_type = str(record.get("event_type") or "").strip().lower()
        action = str(record.get("action") or "").strip().lower()
        status = str(record.get("status") or "").strip().lower()
        message = str(record.get("message") or "")

        delete_hit = self.__matches(message, self._delete_patterns)
        add_hit = self.__matches(message, self._add_patterns)

        # event_type/action 命中删除语义时优先判定为删除。
        if self.__is_delete_event(event_type, action, status) and (delete_hit or self.__is_delete_event_strong(event_type)):
            return "delete"
        if self.__is_add_event(event_type, action, status) and (add_hit or self.__is_add_event_strong(event_type)):
            return "add"

        # 没有结构化字段时，完全依据文案判定；删除优先于新增。
        if delete_hit:
            return "delete"
        if add_hit:
            return "add"
        return None

    @staticmethod
    def __is_delete_event(event_type: str, action: str, status: str) -> bool:
        """判断结构化字段是否指向删除语义。"""
        tokens = f"{event_type} {action} {status}"
        return any(word in tokens for word in ("delete", "remove", "unsubscribe", "cancel"))

    @staticmethod
    def __is_add_event(event_type: str, action: str, status: str) -> bool:
        """判断结构化字段是否指向新增语义。"""
        tokens = f"{event_type} {action} {status}"
        return any(word in tokens for word in ("add", "create", "subscribe"))

    @staticmethod
    def __is_delete_event_strong(event_type: str) -> bool:
        """event_type 本身足够明确时，可独立作为删除依据。"""
        return bool(event_type) and any(
            word in event_type for word in ("delete", "remove", "unsubscribe", "deleted")
        )

    @staticmethod
    def __is_add_event_strong(event_type: str) -> bool:
        """event_type 本身足够明确时，可独立作为新增依据。"""
        return bool(event_type) and any(
            word in event_type for word in ("add", "create", "subscribe")
        )

    @staticmethod
    def __matches(text: str, pattern: str) -> bool:
        """按用户配置的正则匹配文本，正则非法时退化为子串匹配。"""
        if not text or not pattern:
            return False
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return pattern.lower() in text.lower()

    def __extract_media(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """
        从日志记录中提取媒体信息。

        同时读取 ``context`` 结构化字段与 ``message`` 文本，前者更准确，后者作兜底。
        """
        context = record.get("context")
        if not isinstance(context, dict):
            context = {}
        message = str(record.get("message") or "")
        # 把 context 拼成文本，供正则统一匹配。
        context_text = " ".join(f"{key}={value}" for key, value in context.items())

        # TMDB ID：优先结构化字段，其次正则。
        media_id: Optional[str] = None
        for key in TMDB_KEYS:
            value = context.get(key)
            if isinstance(value, (int, str)) and str(value).strip().isdigit():
                media_id = str(value).strip()
                break
        if not media_id:
            for text in (context_text, message):
                match = TMDB_ID_RE.search(text) or TMDB_BARE_RE.search(text)
                if match:
                    media_id = match.group(1)
                    break

        # 标题：优先结构化字段，其次书名号/引号，最后取“关键词：标题”式的冒号后半段。
        title: Optional[str] = None
        for key in TITLE_KEYS:
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        if not title:
            match = QUOTED_TITLE_RE.search(message) or QUOTED_TITLE_RE.search(context_text)
            if match:
                title = match.group(1).strip()
        if not title:
            title = self.__title_after_keyword(message)

        # 年份：优先结构化字段。
        year: Optional[str] = None
        for key in YEAR_KEYS:
            value = context.get(key)
            if isinstance(value, (int, str)):
                candidate = str(value).strip()
                if YEAR_RE.fullmatch(candidate):
                    year = candidate
                    break
        if not year:
            # 标题里带年份时一并取出，便于识别。
            match = YEAR_RE.search(message)
            if match:
                year = match.group(0)

        # 媒体类型：优先结构化字段。
        raw_type: Optional[str] = None
        for key in TYPE_KEYS:
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                raw_type = value.strip()
                break

        return {
            "media_source": MediaSource.TMDB.value if media_id else None,
            "media_id": media_id,
            "title": title,
            "year": year,
            "raw_type": raw_type,
        }

    def __resolve_media_type(self, media: Dict[str, Any], record: Dict[str, Any]) -> Optional[MediaType]:
        """把日志里的类型标识转换为 MoviePilot 的 MediaType。"""
        raw = str(media.get("raw_type") or "").strip().lower()
        if raw in ("movie", "film", "电影"):
            return MediaType.MOVIE
        if raw in ("tv", "series", "episode", "电视剧", "剧集"):
            return MediaType.TV

        override = str(self._default_type or "auto").strip().lower()
        if override == "movie":
            return MediaType.MOVIE
        if override == "tv":
            return MediaType.TV
        # 自动模式交给 MoviePilot 按标题识别。
        return None

    @staticmethod
    def __parse_timestamp(value: Any) -> Optional[datetime]:
        """解析日志时间戳，支持常见几种格式。"""
        if not value or not isinstance(value, str):
            return None
        text = value.strip()
        # 时间戳后面可能被追加了上下文后缀（如 " | k=v"），先截到秒级主体。
        match = TIMESTAMP_PREFIX_RE.match(text)
        if match:
            text = match.group(0)
        for fmt in (
            "%Y-%m-%d %H:%M:%S,%f",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        # 形如 2025-01-01T10:00:00+08:00 的时间戳，剥离时区后返回本地朴素时间。
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def __title_after_keyword(message: str) -> Optional[str]:
        """
        从“关键词：标题”式文案中取出标题。

        Media Automator 的订阅类日志常见写法是关键词后用中英文冒号或破折号接标题，
        例如“已删除订阅：黑袍纠察队,2019”。这里取出后半段并清掉年份等尾缀，
        只保留可用于识别的标题主体。
        """
        if not message:
            return None
        match = COLON_TITLE_RE.search(message)
        if not match:
            return None
        candidate = match.group(1).strip()
        # 去掉尾部仅用于区分的标点与括号内容，避免把备注当成标题的一部分。
        candidate = candidate.strip(" 　.。!！~～")
        # 形如“黑袍纠察队,2019”时，逗号后通常是年份，标题只取前半段。
        head = re.split(r"[,，;；]", candidate, maxsplit=1)[0].strip()
        if head:
            candidate = head
        return candidate or None

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def __persist_state(self) -> None:
        """把读取位置、远程行指纹与历史记录写入插件存储。"""
        self.save_data("cursor", self._cursor)
        self.save_data("http_state", self._http_state)
        self.save_data("history", self._history[-self._history_limit:])

    def __record_history(self, action: str, media: Dict[str, Any], message: str) -> None:
        """
        追加一条处理历史并立即落盘。

        立即保存而不是攒到本轮结束：宿主在保存配置、更新插件时会重入
        ``init_plugin()``，那时会用插件存储里的内容覆盖内存状态；只在
        ``sync()`` 末尾保存会让这批历史丢失。
        """
        with self._state_lock:
            self._history.append(
                {
                    "action": action,
                    "title": media.get("title"),
                    "year": media.get("year"),
                    "media_source": media.get("media_source"),
                    "media_id": media.get("media_id"),
                    "message": message,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]
            # 立即写入插件存储，避免宿主重入 init_plugin 时丢失这部分历史。
            self.__persist_state()

    def __notify_result(self, result: Dict[str, Any]) -> None:
        """发送一条同步结果通知。"""
        from app.schemas.types import MessageType

        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title="Media Automator 订阅同步",
                text=(
                    f"新增订阅：{result['added']} 条\n"
                    f"删除订阅：{result['deleted']} 条\n"
                    f"跳过：{result['skipped']} 条\n"
                    f"失败：{result['failed']} 条"
                ),
            )
        except Exception as error:
            logger.error(f"Media Automator 订阅同步：发送通知失败：{error}")

    def __update_config(self) -> Optional[bool]:
        """回写当前配置。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "log_source": self._log_source,
                "log_path": self._log_path,
                "ma_url": self._ma_url,
                "ma_username": self._ma_username,
                "ma_password": self._ma_password,
                "ma_token": self._ma_token,
                "ma_login_path": self._ma_login_path,
                "ma_timeout": self._ma_timeout,
                "ma_verify_ssl": self._ma_verify_ssl,
                "cron": self._cron,
                "sync_add": self._sync_add,
                "sync_delete": self._sync_delete,
                "default_type": self._default_type,
                "add_patterns": self._add_patterns,
                "delete_patterns": self._delete_patterns,
                "max_lines": self._max_lines,
                "max_age_days": self._max_age_days,
                "history_limit": self._history_limit,
            }
        )

    @staticmethod
    def _parse_non_negative_int(value: Any, fallback: int) -> int:
        """把配置值解析为非负整数，非法时返回默认值（0 合法，表示不限制）。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed >= 0 else fallback

    def __notify_reconcile(self, summary: str) -> None:
        """发送查缺补漏结果通知。"""
        from app.schemas.types import MessageType

        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title="Media Automator 查缺补漏",
                text=summary,
            )
        except Exception as error:
            logger.error(f"Media Automator 查缺补漏：发送通知失败：{error}")

    @staticmethod
    def _parse_positive_int(value: Any, fallback: int) -> int:
        """把配置值解析为正整数，非法时返回默认值。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback
