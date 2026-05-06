"""
数据监控 Python 调度器

调度器职责（简化后）：
1. 定时调度（解析 cron 表达式，注册 APScheduler 任务）
2. 解析 CLI 参数
3. 找到需要执行的 task
4. 调用 core.task_runner.run_task 执行
5. 不再直接执行任务、拼 prompt、调用 Claude、解析 SUMMARY_JSON

所有任务执行逻辑统一由 core.task_runner.run_task() 处理。
"""

import os
import sys
import yaml
import argparse
import logging
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# ============================================================
# 初始化
# ============================================================
BASE_DIR = Path(__file__).resolve().parent          # claude/
ROOT_DIR = BASE_DIR.parent                          # data-monitor/
load_dotenv(ROOT_DIR / ".env")

with open(ROOT_DIR / "config.yaml") as f:
    CONFIG = yaml.safe_load(f)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ============================================================
# 辅助函数
# ============================================================
def _is_disabled(val):
    return str(val).lower() in ["false", "none", "null", ""]


def _str_to_bool(val: str) -> bool:
    """将字符串转换为布尔值"""
    return str(val).lower() not in ["false", "0", "no", "null", ""]


# ============================================================
# 加载任务配置
# ============================================================
def load_task_config(task_name: str) -> dict:
    """加载任务配置（用于判断是否需要定时调度）"""
    conf = {
        "schedule": "0 9 * * 1-5",
        "budget": 0.50,
        "max_turns": 15,
        "timeout": 600,
        "alert_webhook_env": "ALERT_WEBHOOK",
        "default_db_host": "EOS_DB_HOST",
    }

    global_defaults = CONFIG.get("global_defaults", {})
    conf.update(global_defaults)

    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    if task_file.exists():
        content = task_file.read_text(encoding="utf-8")
        if content.startswith("---"):
            try:
                end_pos = content.find("---", 3)
                if end_pos != -1:
                    frontmatter_text = content[3:end_pos]
                    frontmatter = yaml.safe_load(frontmatter_text)
                    if isinstance(frontmatter, dict):
                        for k, v in frontmatter.items():
                            if v is False:
                                continue
                            conf[k] = v
            except Exception as e:
                logger.warning(f"解析 {task_name} Frontmatter 失败: {e}")

    return conf


# ============================================================
# 解析 cron 表达式
# ============================================================
def parse_cron(expr: str) -> dict:
    parts = expr.strip().split()
    return {
        "minute":     parts[0],
        "hour":       parts[1],
        "day":        parts[2],
        "month":      parts[3],
        "day_of_week": parts[4],
    }


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Data Monitor Scheduler")
    parser.add_argument("--task", help="立即运行指定的任务名称 (stem)")
    parser.add_argument("--repo", help="指定要扫描的仓库 ID")
    parser.add_argument("--branch", help="临时覆盖仓库分支")
    parser.add_argument("--notify", default="true",
                        help="是否发送企微通知 (true/false，默认 true)")
    args = parser.parse_args()

    if args.task:
        # 手动执行：调用 core.task_runner.run_task
        from core.task_runner import run_task
        run_task(
            task=args.task,
            trigger_source="manual",
            repo=args.repo,
            branch=args.branch,
            notify=_str_to_bool(args.notify),
        )
        return

    # 定时调度
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    tasks_dir = ROOT_DIR / "tasks"
    defaults = CONFIG.get("global_defaults", {})

    for task_file in tasks_dir.glob("*.md"):
        if task_file.name.startswith("_"):
            continue

        task_name = task_file.stem
        task_conf = load_task_config(task_name)

        schedule_expr = task_conf.get("schedule", defaults.get("schedule", "0 9 * * 1-5"))

        if _is_disabled(schedule_expr) or str(schedule_expr).lower() == "manual":
            logger.info(f"已加载手动任务: {task_name} (不加入定时计划)")
            continue

        try:
            cron_kwargs = parse_cron(schedule_expr)

            # 使用 lambda 闭包传递 task_name，避免 APScheduler 序列化问题
            def make_job(task_name):
                def job():
                    from core.task_runner import run_task
                    run_task(task=task_name, trigger_source="schedule", notify=True)
                return job

            scheduler.add_job(
                make_job(task_name),
                CronTrigger(**cron_kwargs),
                id=task_name,
                name=task_name,
                replace_existing=True
            )
            logger.info(f"已注册定时任务: {task_name} | schedule: {schedule_expr}")
        except Exception as e:
            logger.error(f"任务 {task_name} 的 Cron 表达式解析失败 [{schedule_expr}]: {e}")

    # 日志清理调度
    try:
        sys.path.append(str(ROOT_DIR))
        from cleanup_logs import cleanup_all_logs

        scheduler.add_job(
            cleanup_all_logs,
            trigger="cron",
            month="1,4,7,10",
            day=1,
            hour=2,
            minute=0,
            id="cleanup_logs",
        )
        logger.info("已注册任务: cleanup_logs | schedule: 每3个月(1,4,7,10月)1号凌晨2点")
    except Exception as e:
        logger.warning(f"注册日志清理任务失败: {e}")

    logger.info("调度器启动，按 Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")


if __name__ == "__main__":
    main()
