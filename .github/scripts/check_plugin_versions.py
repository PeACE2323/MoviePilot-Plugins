"""发布前门禁：校验插件版本三处一致，并检查目录/类名约定。

对应开发指南第 11.1 节的 check_plugin_versions.py 的核心规则。
"""

import json
import re
import sys
from pathlib import Path

# 脚本位于 <repo>/.github/scripts/，因此仓库根要上溯三层。
ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins.v3" / "mediaautomatorsync"
INIT = PLUGIN_DIR / "__init__.py"


def main() -> int:
    """执行检查，返回退出码。"""
    src = INIT.read_text(encoding="utf-8")

    class_name = re.search(r"^class\s+(\w+)\s*\(", src, re.M).group(1)
    plugin_version = re.search(r'plugin_version\s*=\s*"([^"]+)"', src).group(1)

    index = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    assert class_name in index, f"package.v3.json 缺少 {class_name} 条目"
    entry = index[class_name]
    index_version = entry["version"]
    history_top = list(entry["history"].keys())[0]

    print(f"类名            : {class_name}")
    print(f"目录名          : {PLUGIN_DIR.name}")
    print(f"plugin_version  : {plugin_version}")
    print(f"索引 version    : {index_version}")
    print(f"history 首项    : {history_top}")

    ok = True
    # 目录名必须是类名的小写形式
    if PLUGIN_DIR.name != class_name.lower():
        print("✗ 目录名不是类名的小写形式")
        ok = False
    # 三处版本必须一致
    if not (plugin_version == index_version == history_top.lstrip("v")):
        print("✗ plugin_version / 索引 version / history 首项不一致")
        ok = False
    # history 首项应使用 v 前缀
    if not history_top.startswith("v"):
        print("✗ history 首项缺少 v 前缀")
        ok = False
    # 必需元数据
    for key in ("name", "description", "version", "icon", "author", "history"):
        if key not in entry:
            print(f"✗ 元数据缺少 {key}")
            ok = False

    print("OK: 全部检查通过" if ok else "检查未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
