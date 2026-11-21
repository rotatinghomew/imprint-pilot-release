"""印纹试产留样与放行分析包。

模块按工坊的决策环节划分：

* ``model``    —— 单一项目的数据装载、预检与索引
* ``process``  —— 有共同材料与纹路支持的工艺对比、证据类型区分
* ``rework``   —— 同件原样返工复查的判读（改善／换料掩盖／无法区分）
* ``aging``    —— 老化看板（未到期／失联／成熟不合格等分别显示）
* ``release``  —— 批次级异常后的放行追溯与保留范围
* ``planning`` —— 机时与留样受限下的方案对比
* ``ingest``   —— 新复查结果入口与判断变化追踪
"""

from .model import Project, ProjectError, load_project

__all__ = ["Project", "ProjectError", "load_project"]
