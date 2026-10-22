#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交互式写入 /root/tjise-login/credentials.json（权限 600）。

为什么单独落成脚本：`python3 - <<'PY'` 的写法里，stdin 已经被脚本占用，
再调用 input() 只会读到脚本自身的剩余内容并抛 EOFError——必须让 python
从一个真实文件启动，stdin 才留给键盘。

用法：
  python3 /root/tjise-login/set_credentials.py            # 写正式文件
  python3 /root/tjise-login/set_credentials.py /tmp/x.json # 指定路径（自测用）

@author LiXinwen
"""
import getpass
import json
import os
import sys

DEFAULT_PATH = "/root/tjise-login/credentials.json"


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    try:
        username = input("账号/学号: ").strip()
        password = getpass.getpass("密码（不回显）: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未写入任何文件")
        return 1
    if not username or not password:
        print("账号或密码为空，未写入任何文件")
        return 1

    tmp = target + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"username": username, "password": password}, f, ensure_ascii=False)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    mode = os.stat(target).st_mode & 0o777
    print("已写入 %s：权限 %o，%d 字节（账号与密码不在此打印）"
          % (target, mode, os.path.getsize(target)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
