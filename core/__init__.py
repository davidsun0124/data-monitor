"""
Core module - 框架级能力

提供跨执行器的统一任务执行入口。
"""

from .task_runner import run_task

__all__ = ["run_task"]
