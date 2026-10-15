"""不依赖 pytest 的测试运行器。

DSH 沙箱下无法向托管 Python 安装 pytest，因此这里实现一个极简的运行器：
收集模块内所有 ``test_*`` 函数，对声明了 ``tmp_path`` 参数的用例注入临时目录，
执行并汇总结果。用法：

    python tests/v3/mediaautomatorsync/run_tests.py

在具备 pytest 的环境中，直接 ``pytest tests/v3/mediaautomatorsync`` 亦可。
"""

import importlib.util
import inspect
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_module():
    """以文件名加载测试模块，避免依赖包路径。"""
    spec = importlib.util.spec_from_file_location("test_plugin", HERE / "test_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """执行全部用例并返回进程退出码。"""
    module = _load_module()
    tests = [
        (name, obj)
        for name, obj in vars(module).items()
        if name.startswith("test_") and callable(obj)
    ]
    # 按定义顺序执行，便于定位失败点。
    tests.sort(key=lambda item: inspect.getsourcelines(item[1])[1])

    passed, failed = 0, []
    for index, (name, func) in enumerate(tests):
        params = inspect.signature(func).parameters
        try:
            # 每个用例开始前清空假 HTTP 路由，避免用例之间相互污染。
            reset = getattr(module, "_reset_routes", None)
            if reset is not None:
                reset()
            kwargs = {}
            if "plugin" in params:
                # plugin fixture 在无 pytest 时是普通函数，这里直接调用。
                kwargs["plugin"] = module.plugin()
            if "tmp_path" in params:
                # 直接在工作区内建目录，不用 tempfile：沙箱下系统临时目录的
                # cleanup 会因权限问题失败，而工作区内的目录是允许写入的。
                # 每个用例固定复用自己的目录，避免残留目录越积越多。
                base = HERE / ".tmp"
                base.mkdir(exist_ok=True)
                run_dir = base / f"case_{index}"
                # 清空内容但保留目录本身，规避某些环境下删除目录被拒的问题。
                if run_dir.exists():
                    for child in run_dir.iterdir():
                        child.unlink(missing_ok=True)
                run_dir.mkdir(parents=True, exist_ok=True)
                kwargs["tmp_path"] = run_dir
            func(**kwargs)
            passed += 1
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001 - 运行器需要捕获全部失败
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()

    print(f"\n{passed} passed, {len(failed)} failed, {passed + len(failed)} total")
    if failed:
        print("失败用例：" + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
