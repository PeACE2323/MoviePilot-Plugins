"""不依赖 pytest 的测试运行器。

DSH 沙箱下无法向托管 Python 安装 pytest，因此这里实现一个极简的运行器：
收集模块内所有 ``test_*`` 函数，对声明了 ``tmp_path`` 参数的用例注入临时目录，
执行并汇总结果。用法：

    python tests/v3/mediaautomatorsync/run_tests.py

在具备 pytest 的环境中，直接 ``pytest tests/v3/mediaautomatorsync`` 亦可。
"""

import importlib.util
import inspect
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _temp_root() -> Path:
    """挑一个当前进程确实能写的临时根目录。

    优先仓库内的 ``.tmp``，便于随仓库清理；但工作区权限继承异常的环境
    （例如目录由别的会话创建，当前沙箱身份拿不到写入授权）会拒绝写入，
    此时回退到系统临时目录，避免整套用例因为环境问题集体失败。
    """
    candidates = [HERE / ".tmp", Path(tempfile.gettempdir()) / "moviepilot-plugin-tests"]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        return candidate
    raise RuntimeError(f"找不到可写的临时目录，已尝试：{candidates}")


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
                # 每个用例固定复用自己的目录，避免残留目录越积越多；
                # 目录本身保留不删，规避某些环境下删除目录被拒的问题。
                run_dir = _temp_root() / f"case_{index}"
                if run_dir.exists():
                    for child in run_dir.iterdir():
                        try:
                            if child.is_dir():
                                shutil.rmtree(child, ignore_errors=True)
                            else:
                                child.unlink(missing_ok=True)
                        except OSError:
                            # 清理失败不影响用例：插件按绝对路径读写，
                            # 残留文件不会串到下一个用例里。
                            pass
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
