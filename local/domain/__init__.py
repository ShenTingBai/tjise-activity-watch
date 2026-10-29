# -*- coding: utf-8 -*-
"""领域层：纯规则与纯数据模型。

依赖方向约束（本包必须始终满足）：
- 不读文件、不联网、不开进程、不碰 pywebview / DOM，也不读墙钟（此刻值由调用方传入）；
- 只接受值并返回值，所有外部副作用由 application 编排、infrastructure 承担；
- 只依赖标准库与本包自身；时间换算、模式分类等一律由 composition root
  （local/app.py）以参数注入，不直接引用 time_contract / signup_plan 等混合模块。

@author LiXinwen
"""
