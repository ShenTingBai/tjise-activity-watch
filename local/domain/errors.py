# -*- coding: utf-8 -*-
"""领域层共享语义类型（无行为，仅标识）。

@author LiXinwen
"""


class ClosingError(RuntimeError):
    """Api 关闭屏障拒绝新 worker 创建：属「取消/正在关闭」语义。

    调用方必须把它与组件异常分开处理——关闭拒绝不得归类为 worker_* 组件码
    （重下/重检都解决不了「程序正在退出」），也不得冒充报名失败向上抛。
    """
