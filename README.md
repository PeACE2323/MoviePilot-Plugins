# MoviePilot-Plugins（个人插件仓库）

存放自用的 [MoviePilot](https://github.com/jxxghp/MoviePilot) V3 插件。

仓库结构遵循官方约定：插件源码位于 `plugins.v3/<插件ID小写>/`，市场索引为根目录的
`package.v3.json`，测试位于 `tests/v3/<插件ID小写>/`。

## 插件列表

| 插件 ID | 名称 | 版本 | 说明 |
| --- | --- | --- | --- |
| `MediaAutomatorSync` | [Media Automator 订阅同步](plugins.v3/mediaautomatorsync/README.md) | 1.1.0 | 读取 Media Automator 日志，自动同步订阅的新增与删除 |

## 安装方式

在 MoviePilot 的环境变量里加入本仓库（多个仓库用英文逗号分隔）：

```yaml
environment:
  - PLUGIN_MARKET=jxxghp/MoviePilot-Plugins,PeACE2323/MoviePilot-Plugins
```

重启 MoviePilot 后，在「插件」页面即可搜索并安装。

## 目录说明

```text
MoviePilot-Plugins/
├── plugins.v3/              # V3 插件源码
│   └── mediaautomatorsync/
│       ├── __init__.py      # 插件主类
│       ├── client.py        # 远程日志客户端
│       └── README.md        # 使用说明
├── tests/v3/                # 插件测试
│   └── mediaautomatorsync/
├── icons/                   # 插件图标
├── package.v3.json          # V3 插件市场索引
└── .github/scripts/         # 发布前检查脚本
```

## 开发

插件依赖 MoviePilot 宿主环境。运行测试：

```bash
# 使用宿主虚拟环境
../MoviePilot/.venv/bin/python -m pytest tests/v3/mediaautomatorsync

# 或不依赖 pytest 的运行器
python tests/v3/mediaautomatorsync/run_tests.py
```

发布前校验版本一致性（插件类 `plugin_version`、索引 `version`、`history` 首项三处必须一致）：

```bash
python .github/scripts/check_plugin_versions.py
```
