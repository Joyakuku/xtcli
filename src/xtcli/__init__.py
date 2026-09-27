"""xtcli —— 嵌入式工程 CLI（Python 实现）。

架构: core（芯片无关）+ backends（按工具链生态划分）。
core 不认识任何芯片, 只认识 Backend 约定的六个动作。
"""

__version__ = "0.2.0"
