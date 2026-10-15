# Media Automator 订阅同步（MediaAutomatorSync）

MoviePilot V3 插件。读取 [Media Automator](https://media-automator.gitbook.io/media_automator-docs)
的 `app.log`，当 Media Automator 中新增或删除订阅时，自动在 MoviePilot 上镜像同样的操作。

## 它解决什么问题

Media Automator 与 MoviePilot 都是「订阅 → 搜索 → 推送 → 归档」型工具。如果你同时用
两者，订阅需要在两边各维护一份：在 Media Automator 里通过 TG 指令 `订阅 xxx` 或
Web UI 加了一条，MoviePilot 这边还是空的；在 Media Automator 里删掉一条，MoviePilot
仍会继续搜索和下载。

本插件把 Media Automator 的订阅变更单向同步到 MoviePilot，让你只在 Media Automator
一处维护订阅。

## 工作原理

Media Automator 的日志由 `app/core/logging.py` 中的 `CustomJsonFormatter` 写出，
**每行是一个独立 JSON 对象**，形如：

```json
{"timestamp": "2025-10-28 12:00:00", "level": "INFO", "log_module": "telegram",
 "message": "订阅成功", "event_type": "subscriptions_changed"}
```

因此插件不是去猜中文文案，而是结构化解析每一行：

1. **取得日志**：两种来源二选一——
   * 远程 HTTP：登录 Media Automator 后调用 `GET /api/system/logs/download` 拉取 `app.log`；
   * 本地文件：读共享文件，记录偏移量与 `(st_dev, st_ino)` 身份。
2. **定位增量**：文件模式按字节偏移量；HTTP 模式因为每次都拿到整份文件，改用
   「上一批行数 + 末行指纹」判断新增区段，日志被轮转时自动降级为按最近 N 行处理。
3. **事件分类**：优先依据 `event_type` / `action` / `status` 等结构化字段判断新增或删除，
   再用可配置的正则匹配 `message` 兜底。
4. **媒体提取**：优先读结构化 `context`（`tmdb_id` / `title` / `year` / `type`），
   否则从文案里正则提取 TMDB ID、书名号标题与年份。
5. **执行同步**：新增走 `SubscribeChain().add(...)`（优先按 TMDB 身份，失败回退按标题）；
   删除走 `SubscribeOper` 查询后 `delete(sid)`（优先按 TMDB 身份，否则按标题包含匹配）。
6. **状态持久化**：读取位置、行指纹与处理历史写入插件数据目录，崩溃或重启不会重复处理，
   也不会丢事件。

每天只处理最近 N 天（默认 30 天）的日志，避免首次运行把陈年旧账一次性导入。

## 安装与配置

插件支持两种日志来源，在「日志来源」下拉里选择。

### 方式一：远程 HTTP（推荐，无需挂载）

只要 MoviePilot 能访问到 Media Automator 的地址，填上地址与账号密码即可：

| 配置项 | 示例 |
| --- | --- |
| 日志来源 | 远程 HTTP |
| Media Automator 地址 | `http://192.168.6.2:8010` |
| 用户名 | `root` |
| 密码 | 你的登录密码 |
| Token（可选） | 留空即用账号密码登录 |
| 登录接口路径（可选） | 留空自动探测 |
| 超时(秒) | 30 |
| 日志轮询周期 | `*/2 * * * *`（建议 2~5 分钟） |

**关于登录接口的自动探测**：Media Automator 没有公开的 API 文档，插件不知道登录端点
的确切路径，因此按以下顺序查找：

1. 你在「登录接口路径」里手填的路径；
2. 读取 `/openapi.json`（FastAPI 默认开放），找出路径名含 `login` / `token` /
   `signin` 的端点；
3. 依次尝试 `/api/login`、`/api/system/login`、`/api/auth/login`、`/api/user/login`、
   `/api/token`、`/api/system/token`。

若全部失败，插件会在错误信息里列出尝试过的路径。此时用浏览器 F12 抓一次登录请求
（Network 面板 → 点登录 → 看 POST 请求的 URL 路径），把路径填进「登录接口路径」即可。

**Token 说明**：登录得到的 Token 缓存在内存里跨轮询复用，不会每次都登录；遇到
401/403 会清掉 Token 自动重新登录并重试一次。也可以直接在配置里填一个现成 Token
（优先级高于账号密码），适合临时排查。

> 注意：下载接口每次返回**整份日志文件**（实测约 1.85 MB），所以轮询周期不建议设得太密。

### 方式二：本地文件（需要挂载）

如果不想把账号密码交给插件，或希望完全不走网络，把日志目录以只读方式挂载进来：

```yaml
services:
  moviepilot:
    volumes:
      # 左侧为宿主机上 Media Automator 的日志目录，右侧为容器内挂载点
      - /path/to/media_automator/data/logs:/ma-logs:ro
```

然后「日志来源」选「本地文件」，「日志文件路径」填 `/ma-logs/app.log`。

Media Automator 容器内的日志固定位于 `/data/logs/`，主文件是 `app.log`（按 `最大体积` /
`备份数量` 轮转为 `app.log.1` 等）。

### 其余配置项

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 总开关 |
| 发送通知 | 有变更时推送汇总通知 |
| 立即运行一次 | 保存后立刻执行一次 |
| 同步新增订阅 / 同步删除订阅 | 分别控制两个方向 |
| 无法判断类型时 | `auto`（按标题识别）/ `movie` / `tv` |
| 新增 / 删除匹配正则 | 日志文案匹配规则，按你的版本调整 |
| 单次最多处理行数 | 防止一次导入过量历史（默认 2000） |
| 只处理最近 N 天 | 默认 30 |
| 保留历史记录条数 | 详情页展示条数，默认 200 |

### 验证配置

保存配置后先调用「测试连接」：

```bash
curl -H "X-API-KEY: <你的 API_TOKEN>" \
  "http://<moviepilot>:3000/api/v1/plugin/MediaAutomatorSync/test"
```

成功会返回日志大小；失败会说明是地址不通、登录失败还是读取被拒。

### 核对正则是否匹配你的版本

Media Automator 的订阅业务代码以编译产物（`.so`）分发，**无法从源码逐一核对日志文案**，
不同版本的措辞也可能不同。因此插件提供了「预览解析」能力：

```bash
curl -H "X-API-KEY: <你的 API_TOKEN>" \
  "http://<moviepilot>:3000/api/v1/plugin/MediaAutomatorSync/probe?limit=50"
```

返回最近若干行日志及插件的判定结果（`detected_action`、`event_type` 与 `media`）。
如果发现 `detected_action` 为空但实际是订阅事件，把该行 `message` 的特征词补进
「新增/删除匹配正则」即可。

## 关键设计说明

- **不直接操作宿主 Model**：新增订阅走 `SubscribeChain`，只读查询与删除走
  `SubscribeOper`，符合 V3「宿主数据只经 Oper / Chain / 稳定 SDK 访问」的约束。
- **重复初始化安全**：`init_plugin()` 每次都从插件存储重建状态，符合宿主保存配置、
  更新插件、创建分身时反复调用的事实。
- **单条失败不影响整批**：每条事件独立 try/except 并计入 `failed`，不会因一条识别
  失败而中断本轮同步。
- **不写回源码目录**：运行数据通过 `save_data()` 存放在插件数据目录。
- **不持有后台线程**：轮询交给宿主调度器（`get_service()`），`stop_service()` 只落盘状态。
- **HTTP 客户端按调用时解析**：`client.py` 通过 `app.sdk.network` 模块属性取
  `AsyncRequestUtils`，而不是在导入期绑定类，这样宿主替换实现后仍然生效。

## 已知限制

1. **Media Automator 没有官方订阅 API**。其文档中的「影巢 API」实为第三方 HDHive 的
   开放接口，与订阅无关；订阅也没有对外 REST 端点。因此读日志是目前唯一可行的集成
   路径（另一条是直接读它的 SQLite `media_automator.db`，但表结构未公开且有锁风险）。
2. **登录接口路径需要探测**。Media Automator 未公开 API 文档，插件只能按
   openapi.json + 常见路径查找；探测失败时需手动抓包填入路径。
3. **日志文案依赖版本**。默认正则覆盖常见写法，但无法保证匹配所有版本；请用
   `/probe` 核对。`event_type` 判据比文案稳定，但枚举字面值同样在编译产物中，
   插件采用「结构化字段优先 + 文案兜底」的宽松策略。
4. **远程模式每次拉取整份日志**。下载接口不支持增量（内容长度实测约 1.85 MB），
   因此轮询周期不宜过密；插件用行指纹保证不重复处理，但带宽开销是固定的。
5. **删除方向按标题匹配时可能多删**。当日志中没有 TMDB ID 时，插件按
   「标题包含关键词」匹配，这与 Media Automator 自身的删除语义一致（它也会删除标题
   包含关键词的全部订阅），但如果你在 MoviePilot 有同名不同剧的订阅，请注意。
6. **只做单向同步**（Media Automator → MoviePilot）。MoviePilot 侧的新增不会回写，
   以避免两个系统互相回环。

## 测试

```bash
# 仓库根目录，使用 MoviePilot 宿主虚拟环境
../MoviePilot/.venv/bin/python -m pytest tests/v3/mediaautomatorsync

# 无 pytest 环境下的等价运行器
python tests/v3/mediaautomatorsync/run_tests.py
```

35 个用例覆盖：JSON 行解析、纯文本兜底、事件分类（文案 / event_type / 删除优先 /
时间过滤）、媒体提取（context 优先 / 文案正则 / 冒号式标题）、增量读取、日志轮转、
开关控制、非法 cron、失败回退与按标题删除，以及远程模式（Token 提取、登录探测、
增量去重、日志轮转降级、401 自动重登重试、凭证缺失与不可达的错误处理）。

## 插件 API

前缀 `/api/v1/plugin/MediaAutomatorSync`，`auth` 均为 `bear`。

| 路径 | 说明 |
| --- | --- |
| `/run` | 立即执行一次同步 |
| `/status` | 当前来源、地址、读取位置与上次错误 |
| `/test` | 测试连通性与登录（远程模式会真实登录） |
| `/probe?limit=50` | 预览日志解析结果，核对正则 |
| `/reset` | 清空读取位置、行指纹与历史 |
