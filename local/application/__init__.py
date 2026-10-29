# -*- coding: utf-8 -*-
"""应用层：用例编排。

依赖方向约束：
- 本包接收命令、调用 domain 纯策略与注入的 infrastructure 端口、发布结构化事件；
- 不直接调用 Windows API / Playwright / 具体文件路径——需要的端口全部由
  interface（app 外观）以参数注入，测试可整体换成内存替身；
- 只依赖标准库与 domain；signup_plan 纯函数、time_contract 换算、business_clock
  异常类型等混合模块能力一律经参数注入，不直接 import；
- 绝不 import app（app 有导入期副作用，反向引用会成环）。

@author LiXinwen
"""
