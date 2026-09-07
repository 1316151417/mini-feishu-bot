"""可插拔 workflow：注册表见 registry.py，公共接口见 base.py。"""
from workflows.registry import available, build_workflow, register

# 导入以下包以触发注册（各包 __init__ 仅转发 build）
from workflows import dev_review, simple_agent  # noqa: E402,F401
