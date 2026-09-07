"""workflow 注册表：按名称构建，新增 workflow 用 @register 登记即可插拔。"""
from __future__ import annotations

from typing import Callable

from workflows.base import Workflow, WorkflowContext

_REGISTRY: dict[str, Callable[[WorkflowContext], Workflow]] = {}


def register(name: str) -> Callable[[Callable[[WorkflowContext], Workflow]],
                                    Callable[[WorkflowContext], Workflow]]:
    def decorator(factory: Callable[[WorkflowContext], Workflow],
                  ) -> Callable[[WorkflowContext], Workflow]:
        _REGISTRY[name] = factory
        return factory

    return decorator


def available() -> list[str]:
    return sorted(_REGISTRY)


def build_workflow(ctx: WorkflowContext, name: str = "simple") -> Workflow:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"未知 workflow {name!r}，可选：{'、'.join(available())}") from None
    return factory(ctx)


# 底部导入以触发注册（workflows 为命名空间包，无 __init__.py）
import workflows.dev_review.workflow  # noqa: E402,F401
import workflows.simple_agent.workflow  # noqa: E402,F401
