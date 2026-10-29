# -*- coding: utf-8 -*-
"""基础设施层：所有外部副作用的适配器（文件、进程、时钟、下载）。

依赖方向约束：
- 本包只被 interface（app 外观）与 application 使用/注入，绝不反向依赖它们；
- 每个适配器提供可替换接口（显式路径/依赖参数或模块级替身点），测试可整体换桩；
- 可以引用 domain 的纯规则；不得 import app（app 有导入期副作用）。

@author LiXinwen
"""
