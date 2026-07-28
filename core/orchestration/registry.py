"""节点类型注册表。

用于按类型名构造节点（从配置驱动图构建时使用）。
"""
from __future__ import annotations

from typing import Callable, Type

from core.orchestration.node import Node


class NodeRegistry:
    """节点类型注册表。"""

    def __init__(self):
        self._types: dict[str, Type[Node]] = {}

    def register(self, node_class: Type[Node]) -> Type[Node]:
        """注册节点类型。可作为装饰器使用。"""
        type_name = node_class.node_type
        if type_name in self._types:
            raise ValueError(f"节点类型已注册: {type_name}")
        self._types[type_name] = node_class
        return node_class

    def get(self, type_name: str) -> Type[Node]:
        if type_name not in self._types:
            raise KeyError(
                f"节点类型未注册: {type_name}，已注册: {list(self._types.keys())}"
            )
        return self._types[type_name]

    def list_types(self) -> list[str]:
        return list(self._types.keys())


# 全局默认注册表
_default_registry = NodeRegistry()


def get_default_registry() -> NodeRegistry:
    return _default_registry


def register_node_type(node_class: Type[Node]) -> Type[Node]:
    """注册节点类型到默认注册表。可作为装饰器。"""
    return _default_registry.register(node_class)
