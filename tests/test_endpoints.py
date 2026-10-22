"""站点地址来源测试：默认值、SCHOOL_ORIGIN 覆盖、以及各模块是否真的用上了共享地址。

为什么要测：这些地址决定了 token 往哪发、Referer 带哪个站点。
换学校时如果某处还硬编码旧域名，请求会打到旧站点，表现是「接口突然 401」，
而不是「配置没生效」——这种错很难一眼看出来，只能在改配置时扫一遍。

分两层：
- 动态层：每个用例起一个子进程重新 import common 再打印常量，
  不让测试进程自己先被环境变量或已加载的模块状态污染；
- 静态层：直接读源码，确认几个入口脚本里已经不再留学校域名。
  不 import 它们，是因为这些脚本依赖 playwright / 浏览器环境，测试不该被它拖住。

common 的模块级只算常量（load_config 是惰性调用），
所以整个过程不读 config.json、不读 data/、也不建立任何网络连接。
"""
import json
import os
import subprocess
import sys

try:  # Windows 控制台默认 GBK，打 ✓/✗ 会直接抛 UnicodeEncodeError；统一按 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DEV_SCRIPTS = SCRIPTS / "dev"
DEFAULT_ORIGIN = "https://xg.tjise.edu.cn"

# 子进程里只做「导入 + 打印」，不写盘、不联网
COMMON_PROBE = (
    "import json, common;"
    "print(json.dumps({"
    "'origin': common.SCHOOL_ORIGIN,"
    "'api_base': common.API_BASE,"
    "'activity_api': common.ACTIVITY_API,"
    "'home_url': common.HOME_URL,"
    "'referer': common.REFERER,"
    "}))"
)

# 这些文件里出现学校域名就说明还有一处没跟着 SCHOOL_ORIGIN 走
# （登录入口在 scripts/，一次性排查脚本挪到了 scripts/dev/）
NO_HARDCODE_FILES = [
    SCRIPTS / "login.py",
    DEV_SCRIPTS / "explore.py",
    DEV_SCRIPTS / "probe_login.py",
]

# (说明, 子进程里的 SCHOOL_ORIGIN, 期望的 origin)
CASES = [
    ("未设置环境变量时回落到当前学校", None, DEFAULT_ORIGIN),
    ("SCHOOL_ORIGIN 覆盖默认值", "https://demo.example.edu.cn", "https://demo.example.edu.cn"),
    ("结尾斜杠不影响拼接", "https://demo.example.edu.cn/", "https://demo.example.edu.cn"),
    ("带端口的 http origin 原样保留", "http://10.0.0.5:8080", "http://10.0.0.5:8080"),
]

results = []


def check(desc: str, got, expect) -> None:
    ok = got == expect
    results.append((desc, ok))
    print("  %s %-28s 期望 %-46s 实际 %s"
          % ("✓" if ok else "✗", desc, expect, got))


def run_probe(code: str, origin):
    """在干净环境里跑一段探针代码，返回解析后的 dict。"""
    env = dict(os.environ)
    env.pop("SCHOOL_ORIGIN", None)
    if origin is not None:
        env["SCHOOL_ORIGIN"] = origin
    out = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=str(SCRIPTS), env=env, text=True,
        encoding="utf-8", errors="replace",
    )
    return json.loads(out.strip().splitlines()[-1])


def dynamic_checks() -> None:
    for desc, origin, expect_origin in CASES:
        print(desc)
        got = run_probe(COMMON_PROBE, origin)

        check("origin", got["origin"], expect_origin)
        check("API_BASE", got["api_base"], f"{expect_origin}/dr/api")
        check("ACTIVITY_API", got["activity_api"],
              f"{expect_origin}/dr/api/rjyHd/listActivity")
        check("HOME_URL", got["home_url"], f"{expect_origin}/stu/")
        check("REFERER 与 HOME_URL 同源", got["referer"], got["home_url"])

        # 覆盖场景下不能再残留旧域名，否则就是某处还硬编码着
        if origin is not None:
            left = [k for k, v in got.items() if DEFAULT_ORIGIN in str(v)]
            check("覆盖后不残留旧域名", left, [])


def static_checks() -> None:
    print("静态检查：入口脚本里不该再硬编码学校域名")
    for path in NO_HARDCODE_FILES:
        name = str(path.relative_to(ROOT)).replace("\\", "/")
        src = path.read_text(encoding="utf-8")
        check(f"{name} 无硬编码域名", DEFAULT_ORIGIN in src, False)
        check(f"{name} 引用 HOME_URL", "HOME_URL" in src, True)

    refresh = (ROOT / "deploy" / "server_refresh.js").read_text(encoding="utf-8")
    # 只有默认值那一处允许出现学院域名，其余都应从 SCHOOL_ORIGIN 派生
    check("server_refresh.js 域名只作默认值出现一次",
          refresh.count(DEFAULT_ORIGIN), 1)
    check("server_refresh.js 使用 SCHOOL_ORIGIN", "SCHOOL_ORIGIN" in refresh, True)


def main() -> int:
    dynamic_checks()
    static_checks()

    failed = [d for d, ok in results if not ok]
    print("\n共 %d 项，失败 %d 项" % (len(results), len(failed)))
    if failed:
        for f in failed:
            print("  ✗", f)
        return 1
    print("== 通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
