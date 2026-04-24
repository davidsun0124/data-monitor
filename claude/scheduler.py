"""
数据监控 Python 调度器
等价替代 cron + runner.sh，一个进程搞定调度 + 执行 + 日志 + 告警
"""

import subprocess
import os
import sys
import csv
import json
import time
import logging
import requests
import yaml
import argparse
from datetime import datetime
from pathlib import Path
import re
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# 统一处理“关闭”语义的辅助函数
def _is_disabled(val):
    return str(val).lower() in ["false", "none", "null", ""]

# ============================================================
# 初始化
# ============================================================
BASE_DIR = Path(__file__).resolve().parent          # claude/
ROOT_DIR = BASE_DIR.parent                          # data-monitor/（共用文件所在目录）
load_dotenv(ROOT_DIR / ".env")

with open(ROOT_DIR / "config.yaml") as f:
    CONFIG = yaml.safe_load(f)

LOG_DIR = BASE_DIR / "logs"                         # 日志写到 claude/logs/
LOG_DIR.mkdir(exist_ok=True)
SUMMARY_FILE = LOG_DIR / "summary.csv"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")
# 屏蔽 APScheduler 库自带的冗余 INFO 日志，只保留 WARNING 以上级别
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ============================================================
# 加载任务配置（优先级：tasks/*.md Frontmatter > config.yaml > 默认值）
# ============================================================
def load_task_config(task_name: str) -> dict:
    # 1. 基础默认值
    conf = {
        "schedule": "0 9 * * 1-5",
        "budget": 0.50,
        "max_turns": 15,
        "timeout": 600,
        "alert_webhook_env": "ALERT_WEBHOOK",
        "default_db_host": "EOS_DB_HOST",
    }

    # 2. 从 config.yaml 加载（全局默认配置）
    global_defaults = CONFIG.get("global_defaults", {})
    conf.update(global_defaults)

    # 3. 从 Markdown Frontmatter 加载（任务层覆盖，优先级最高）
    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    if task_file.exists():
        content = task_file.read_text(encoding="utf-8")
        if content.startswith("---"):
            try:
                # 寻找第二个 ---
                end_pos = content.find("---", 3)
                if end_pos != -1:
                    frontmatter_text = content[3:end_pos]
                    frontmatter = yaml.safe_load(frontmatter_text)
                    if isinstance(frontmatter, dict):
                        conf.update(frontmatter)
            except Exception as e:
                logger.warning(f"解析 {task_name} Frontmatter 失败: {e}")
    
    return conf


# ============================================================
# 执行任务核心逻辑
# ============================================================
def run_task(task_name: str):
    task_conf = load_task_config(task_name)
    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"{task_name}_{timestamp}.log"

    logger.info(f"开始执行: {task_name}")

    if not task_file.exists():
        logger.error(f"任务文件不存在: {task_file}")
        return

    # 读取 prompt (跳过 Frontmatter 部分)
    raw_content = task_file.read_text(encoding="utf-8")
    if raw_content.startswith("---"):
        end_pos = raw_content.find("---", 3)
        if end_pos != -1:
            prompt = raw_content[end_pos+3:].strip()
        else:
            prompt = raw_content.strip()
    else:
        prompt = raw_content.strip()

    # 用环境变量替换 ${VAR}
    for key, val in os.environ.items():
        prompt = prompt.replace(f"${{{key}}}", val)

    # 注入数据库连接指令
    db_host_var = task_conf.get("db_host")
    if db_host_var is None:
        db_host_var = task_conf.get("default_db_host")
    if _is_disabled(db_host_var):
        db_host_var = None

    if db_host_var:
        db_hint = f"【连接信息】数据库 Host 变量为 `${{{db_host_var}}}`。请依据此变量名及其前缀，在环境中查找对应的 PORT, USER, PASS, NAME 变量进行连接。"
        
        # 如果 prompt 中没有显式写“连接信息”，则注入
        if "连接信息" not in prompt:
            prompt = f"{db_hint}\n\n{prompt}"

    budget = 99999 if _is_disabled(task_conf.get("budget")) else task_conf.get("budget")
    max_turns = 999 if _is_disabled(task_conf.get("max_turns")) else task_conf.get("max_turns")
    task_timeout = None if _is_disabled(task_conf.get("timeout")) else task_conf.get("timeout")

    # Windows 下 claude 是 .CMD 文件，需要通过 cmd /c 调用
    # Prompt 通过 stdin 传入（避免 cmd/c 对特殊字符的转义问题）
    if sys.platform == "win32":
        claude_cmd = ["cmd", "/c", "claude"]
    else:
        claude_cmd = ["claude"]

    cmd = claude_cmd + [
        "-p",  # 无参数时 -p 从 stdin 读取 prompt
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(budget),
        "--output-format", "json",
    ]

    # CLAUDECODE="" 防止子进程继承 Claude Code 交互模式
    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            input=prompt,          # prompt 经 stdin 传入，绕过 cmd /c 特殊字符问题
            capture_output=True,
            text=True,
            timeout=task_timeout,
            env=env,
        )
        exit_code = result.returncode
        raw_output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_output = f"[TIMEOUT] 任务执行超过 {task_timeout}s，已强制终止"
    except Exception as e:
        exit_code = -2
        raw_output = f"[EXCEPTION] {e}"

    duration = int(time.time() - start)

    # ── 解析 JSON 输出 ──────────────────────────────────────
    cost, tokens, subtype, result_text = "N/A", "N/A", "unknown", ""
    try:
        data = json.loads(raw_output.strip())
        cost = data.get("total_cost_usd", "N/A")
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        subtype = data.get("subtype", "unknown")
        result_text = data.get("result", "")

        # error_max_turns / error_max_budget_usd 退出码为 0，需标记为失败
        if exit_code == 0 and subtype.startswith("error_"):
            exit_code = 2
    except Exception:
        pass

    # ── 写任务日志（格式同 runner.sh）──────────────────────
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("========================================\n")
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行: {task_name}\n")
        f.write("========================================\n")
        f.write(raw_output + "\n")
        f.write("\n----------------------------------------\n")
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 执行完成\n")
        f.write(f"  退出码 : {exit_code}\n")
        f.write(f"  子类型 : {subtype}\n")
        f.write(f"  耗时   : {duration}s\n")
        f.write(f"  Tokens : {tokens}\n")
        f.write("----------------------------------------\n")

    # ── 写 summary.csv ──────────────────────────────────────
    write_header = not SUMMARY_FILE.exists()
    with open(SUMMARY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "task", "exit_code", "subtype", "duration_s", "cost_usd", "tokens"])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            task_name, exit_code, subtype, duration, cost, tokens,
        ])

    logger.info(f"完成: {task_name} | 退出码={exit_code} | 子类型={subtype} | 耗时={duration}s | Tokens={tokens}")

    # ── 发送告警 ────────────────────────────────────────────
    send_alert(task_name, exit_code, subtype, duration, tokens, result_text, timestamp, task_conf)


# ============================================================
# 解析 SUMMARY_JSON（等价 runner.sh 的 jq 解析部分）
# ============================================================
def parse_summary_json(result_text: str) -> dict:
    """从 AI 输出中提取 SUMMARY_JSON"""
    if not result_text:
        return {}
    
    # 调试日志：查看输出末尾


    # 方案：寻找最后一个 SUMMARY_JSON: 标记
    marker = "SUMMARY_JSON:"
    if marker in result_text:
        try:
            # 取最后一个标记之后的所有内容
            parts = result_text.rsplit(marker, 1)
            json_str = parts[1].strip()
            
            # 如果后面还跟着一些别的内容（比如 Markdown 的引用），尝试只截取到第一个 }
            if "}" in json_str:
                json_str = json_str[:json_str.rfind("}")+1]
                
            data = json.loads(json_str)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"SUMMARY_JSON 强力解析失败: {e}")
    
    return {}


# ============================================================
# 告警（企微 webhook 通知）
# ============================================================
def send_alert(task_name: str, exit_code: int, subtype: str,
               duration: int, tokens, result_text: str, timestamp: str,
               task_conf: dict = None):
    task_conf = task_conf or {}
    webhook_env = task_conf.get("alert_webhook_env", "ALERT_WEBHOOK")
    
    if str(webhook_env).lower() in ["false", "none", "null", ""]:
        logger.info(f"[{task_name}] 告警已被显式禁用，跳过企微通知")
        return

    webhook = os.getenv(webhook_env)
    
    if not webhook:
        logger.warning(f"{webhook_env} 未配置，跳过告警")
        return

    meta = f"耗时: {duration}s | Tokens: {tokens}"

    need_bot = False
    if subtype == "error_max_turns":
        icon, head = "⚠️", "任务中断（轮次超限）"
        # 尝试提取已完成部分的 SUMMARY_JSON
        partial = parse_summary_json(result_text)
        partial_brief = partial.get("brief", "")
        partial_note = f"\n已完成部分摘要: {partial_brief}" if partial_brief else ""
        body = (
            f"原因: AI 轮次耗尽，巡检未完整执行，结果不可信\n"
            f"处理: 已在 config.yaml 中提高 max_turns，下次执行将自动修复{partial_note}\n"
            f"日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
        )
        need_bot = True
    elif subtype == "error_max_budget_usd":
        icon, head = "⚠️", "任务中断（预算超限）"
        body = (
            f"原因: 单次资源消耗（Tokens/费用）超出预算上限，巡检未完整执行\n"
            f"处理: 请在 config.yaml 中提高 budget\n"
            f"日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
        )
        need_bot = True
    elif exit_code == -1:
        icon, head = "❌", "执行超时"
        body = (
            f"原因: 任务执行超过超时限制被强制终止\n"
            f"处理: 已在 config.yaml 中提高 timeout，下次执行将自动修复\n"
            f"日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
        )
        need_bot = True
    elif exit_code != 0:
        icon, head = "❌", "执行失败（框架错误）"
        body = (
            f"原因: 子进程异常退出（exit_code={exit_code}）\n"
            f"处理: 请检查日志排查环境/权限问题\n"
            f"日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
        )
        need_bot = True
    else:
        summary = parse_summary_json(result_text)
        status = summary.get("status", "UNKNOWN")
        level  = summary.get("level", "UNKNOWN")
        brief  = summary.get("brief", "")
        anomaly_types = "、".join(summary.get("anomaly_types", []))
        top5   = summary.get("top5", [])

        if status in ["PASS", "SUCCESS"]:
            icon, head = "✅", "巡检正常"
            body = f"结果: {brief}\n{meta}"
            need_bot = False
        elif status == "FAIL":
            icon_map = {"CRITICAL": "🔴", "WARN": "🟡"}
            icon = icon_map.get(level, "🟠")
            head = f"巡检异常 [{level}]"
            type_line = f"异常类型: {anomaly_types}\n" if anomaly_types else ""
            top5_lines = ""
            if top5:
                items = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(top5[:5]))
                top5_lines = f"异常明细:\n{items}\n"
            body = f"{type_line}异常说明: {brief}\n{top5_lines}{meta}"
            need_bot = True
        else:
            icon, head = "❓", "结果未知"
            body = f"原因: 巡检摘要解析失败，任务可能异常退出\n日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
            need_bot = True

    content = f"【{task_name}】{icon} {head}\n{body}"

    try:
        requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        logger.info(f"告警已发送: {task_name} {head}")
    except Exception as e:
        logger.error(f"告警发送失败: {e}")


# ============================================================
# 日志清理 (引入全局通用逻辑)
# ============================================================
sys.path.append(str(ROOT_DIR))
from cleanup_logs import cleanup_all_logs



# ============================================================
# 解析 cron 表达式 → APScheduler 参数
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
    args = parser.parse_args()

    # 优先处理单任务运行模式
    if args.task:
        run_task(args.task)
        return

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    # 自动发现 tasks 目录下的所有 .md 任务
    tasks_dir = ROOT_DIR / "tasks"
    defaults = CONFIG.get("global_defaults", {})

    for task_file in tasks_dir.glob("*.md"):
        if task_file.name.startswith("_"):
            continue
            
        task_name = task_file.stem
        task_conf = load_task_config(task_name)
        
        schedule_expr = task_conf.get("schedule", defaults.get("schedule", "0 9 * * 1-5"))
        
        # 如果是 manual 或 false/none 等禁用标志，则不加入定时任务队列
        if str(schedule_expr).lower() == "manual" or _is_disabled(schedule_expr):
            logger.info(f"已加载手动任务: {task_name} (不加入定时计划)")
            continue

        try:
            cron_kwargs = parse_cron(schedule_expr)
            scheduler.add_job(
                run_task, 
                CronTrigger(**cron_kwargs), 
                args=[task_file.name],
                id=task_name,
                replace_existing=True
            )
            logger.info(f"已注册定时任务: {task_name} | schedule: {schedule_expr}")
        except Exception as e:
            logger.error(f"任务 {task_name} 的 Cron 表达式解析失败 [{schedule_expr}]: {e}")

    # 日志清理：每3个月的1号凌晨 2 点
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

    logger.info("调度器启动，按 Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")


if __name__ == "__main__":
    main()
