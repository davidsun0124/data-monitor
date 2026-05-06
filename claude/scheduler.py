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
import re
import shutil
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# 统一处理"关闭"语义的辅助函数
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
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ============================================================
# 加载任务配置（优先级：tasks/*.md Frontmatter > config.yaml > 默认值）
# ============================================================
def load_task_config(task_name: str) -> dict:
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
# 加载仓库配置
# ============================================================
def load_repositories(repositories_config: str) -> list:
    config_path = ROOT_DIR / repositories_config
    if not config_path.exists():
        logger.error(f"仓库配置文件不存在: {config_path}")
        return []

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return data.get("repositories", [])


# ============================================================
# 克隆 Git 仓库
# ============================================================
def clone_repo(repo: dict, branch: str = None, task_timestamp: str = None) -> dict:
    repo_id = repo["id"]
    repo_url = repo["repo_url"]
    token = os.getenv(repo.get("token_env", "GITLAB_TOKEN"))
    clone_base = ROOT_DIR / repo.get("clone_base", "scratch/security-scan")
    branch = branch or repo.get("default_branch", "main")

    timestamp = task_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    checkout_path = clone_base / repo_id / f"owasp-scan_{timestamp}"

    checkout_path.parent.mkdir(parents=True, exist_ok=True)

    if checkout_path.exists():
        shutil.rmtree(checkout_path)

    git_url = repo_url
    if token and "gitlab.example.com" in git_url:
        git_url = git_url.replace("https://", f"https://oauth2:{token}@")

    logger.info(f"克隆仓库 {repo_id} ({branch}) 到 {checkout_path}")

    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch", branch, git_url, str(checkout_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout
            logger.error(f"克隆失败 {repo_id}: {error_msg}")
            return {"repo_id": repo_id, "success": False, "error": error_msg, "checkout_path": None, "commit": None}

        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(checkout_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else "unknown"

        return {
            "repo_id": repo_id,
            "success": True,
            "checkout_path": str(checkout_path),
            "commit": commit,
            "branch": branch,
        }

    except subprocess.TimeoutExpired:
        return {"repo_id": repo_id, "success": False, "error": "克隆超时", "checkout_path": None, "commit": None}
    except Exception as e:
        return {"repo_id": repo_id, "success": False, "error": str(e), "checkout_path": None, "commit": None}


# ============================================================
# 执行本地任务（原有逻辑）
# ============================================================
def run_local_task(task_name: str, task_conf: dict, task_timestamp: str):
    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    log_file = LOG_DIR / f"{task_name}_{task_timestamp}.log"

    logger.info(f"开始执行: {task_name}")

    if not task_file.exists():
        logger.error(f"任务文件不存在: {task_file}")
        return

    raw_content = task_file.read_text(encoding="utf-8")
    if raw_content.startswith("---"):
        end_pos = raw_content.find("---", 3)
        if end_pos != -1:
            prompt = raw_content[end_pos+3:].strip()
        else:
            prompt = raw_content.strip()
    else:
        prompt = raw_content.strip()

    for key, val in os.environ.items():
        prompt = prompt.replace(f"${{{key}}}", val)

    db_host_var = task_conf.get("db_host")
    if db_host_var is None:
        db_host_var = task_conf.get("default_db_host")
    if _is_disabled(db_host_var):
        db_host_var = None

    if db_host_var:
        db_hint = f"【连接信息】数据库 Host 变量为 `${{{db_host_var}}}`。请依据此变量名及其前缀，在环境中查找对应的 PORT, USER, PASS, NAME 变量进行连接。"
        if "连接信息" not in prompt:
            prompt = f"{db_hint}\n\n{prompt}"

    budget = task_conf.get("budget", 0.5)
    max_turns = task_conf.get("max_turns", 15)
    task_timeout = task_conf.get("timeout", 600)

    if sys.platform == "win32":
        claude_cmd = ["cmd", "/c", "claude"]
    else:
        claude_cmd = ["claude"]

    cmd = claude_cmd + [
        "-p",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(budget),
        "--output-format", "json",
    ]

    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=task_timeout,
            env=env,
        )
        exit_code = result.returncode
        raw_output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_output = f"[TIMEOUT] 任务执行超过 {task_timeout}s"
    except Exception as e:
        exit_code = -2
        raw_output = f"[EXCEPTION] {e}"

    duration = int(time.time() - start)

    cost, tokens, subtype, result_text = "N/A", "N/A", "unknown", ""
    try:
        data = json.loads(raw_output.strip())
        cost = data.get("total_cost_usd", "N/A")
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        subtype = data.get("subtype", "unknown")
        result_text = data.get("result", "")
        if exit_code == 0 and subtype.startswith("error_"):
            exit_code = 2
    except Exception:
        pass

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

    send_alert(task_name, exit_code, subtype, duration, tokens, result_text, task_timestamp, task_conf)


# ============================================================
# 执行单个仓库任务
# ============================================================
def run_task_for_repo(task_name: str, task_conf: dict, repo: dict, branch: str, task_timestamp: str):
    repo_id = repo["id"]
    repo_name = repo["name"]
    repo_url = repo["repo_url"]

    logger.info(f"开始扫描仓库: {repo_id}")

    clone_result = clone_repo(repo, branch, task_timestamp)
    if not clone_result["success"]:
        return {
            "repo_id": repo_id,
            "repo_name": repo_name,
            "status": "INTERRUPTED",
            "error": clone_result["error"],
            "findings": [],
            "sensitive_info": [],
            "report_path": None,
        }

    checkout_path = clone_result["checkout_path"]
    commit = clone_result["commit"]
    branch_name = clone_result["branch"]

    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    raw_content = task_file.read_text(encoding="utf-8")
    if raw_content.startswith("---"):
        end_pos = raw_content.find("---", 3)
        if end_pos != -1:
            prompt = raw_content[end_pos+3:].strip()
        else:
            prompt = raw_content.strip()
    else:
        prompt = raw_content.strip()

    for key, val in os.environ.items():
        prompt = prompt.replace(f"${{{key}}}", val)

    prompt = prompt.replace("${SCAN_REPO_ID}", repo_id)
    prompt = prompt.replace("${SCAN_REPO_NAME}", repo_name)
    prompt = prompt.replace("${SCAN_REPO_URL}", repo_url)
    prompt = prompt.replace("${SCAN_BRANCH}", branch_name)
    prompt = prompt.replace("${SCAN_COMMIT}", commit)
    prompt = prompt.replace("${SCAN_CHECKOUT_PATH}", checkout_path)

    budget = task_conf.get("budget", 0.5)
    max_turns = task_conf.get("max_turns", 15)
    task_timeout = task_conf.get("timeout", 600)

    if sys.platform == "win32":
        claude_cmd = ["cmd", "/c", "claude"]
    else:
        claude_cmd = ["claude"]

    cmd = claude_cmd + [
        "-p",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--max-budget-usd", str(budget),
        "--output-format", "json",
    ]

    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=task_timeout,
            env=env,
        )
        exit_code = result.returncode
        raw_output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        exit_code = -1
        raw_output = f"[TIMEOUT] 任务执行超过 {task_timeout}s"
    except Exception as e:
        exit_code = -2
        raw_output = f"[EXCEPTION] {e}"

    summary = parse_summary_json(raw_output)
    summary["repo_id"] = repo_id
    summary["repo_name"] = repo_name
    summary["checkout_path"] = checkout_path

    logger.info(f"仓库扫描完成: {repo_id}, status={summary.get('status', 'UNKNOWN')}")

    return summary


# ============================================================
# 执行任务（支持单仓库或多仓库）
# ============================================================
def run_task(task_name: str, specific_repo: str = None, override_branch: str = None):
    task_conf = load_task_config(task_name)
    task_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    target_type = task_conf.get("target_type", "local")
    target_config = task_conf.get("target_config", "repositories.yaml")

    repos_to_scan = []
    is_git_task = target_type == "git_repositories"

    if is_git_task:
        all_repos = load_repositories(target_config)
        for repo in all_repos:
            if not repo.get("enabled", False):
                continue
            if specific_repo and repo["id"] != specific_repo:
                continue
            if task_name not in repo.get("tasks", []):
                continue
            repos_to_scan.append(repo)
    else:
        if specific_repo:
            logger.warning(f"任务 {task_name} 不是 Git 仓库任务，--repo 参数无效")
        repos_to_scan = [{"id": "local", "name": "Local", "repo_url": "", "tasks": [task_name]}]

    if not repos_to_scan:
        logger.warning(f"没有找到需要扫描的仓库: task={task_name}, repo={specific_repo}")
        return

    if not is_git_task:
        # 本地任务走原有逻辑
        run_local_task(task_name, task_conf, task_timestamp)
        return

    if len(repos_to_scan) == 1:
        result = run_task_for_repo(task_name, task_conf, repos_to_scan[0], override_branch, task_timestamp)
        final_result = {
            "task": task_name,
            "status": result.get("status", "INTERRUPTED"),
            "type": result.get("type", "UNKNOWN"),
            "summary": result.get("summary", ""),
            "reason_short": result.get("reason_short", ""),
            "owasp_source": result.get("owasp_source", "official"),
            "owasp_version": result.get("owasp_version", ""),
            "details": result.get("details", {}),
            "error": result.get("error", {}),
        }
    else:
        repo_results = []
        all_findings = []
        all_sensitive_info = []
        total_high = 0
        total_medium = 0
        total_low = 0
        repo_reports = []

        for repo in repos_to_scan:
            r = run_task_for_repo(task_name, task_conf, repo, override_branch, task_timestamp)
            repo_results.append(r)

            details = r.get("details", {})
            findings = details.get("findings", [])
            sensitive = details.get("sensitive_info", [])
            repo_report_path = details.get("report_path")

            all_findings.extend(findings)
            all_sensitive_info.extend(sensitive)
            total_high += details.get("high_count", 0)
            total_medium += details.get("medium_count", 0)
            total_low += details.get("low_count", 0)

            if repo_report_path and os.path.exists(repo_report_path):
                repo_reports.append({
                    "repo_id": repo["id"],
                    "repo_name": repo["name"],
                    "report_path": repo_report_path,
                })

        has_error = any(r.get("status") == "ERROR" for r in repo_results)
        has_warn = any(r.get("status") == "WARN" for r in repo_results)

        if has_error:
            final_status = "ERROR"
        elif has_warn:
            final_status = "WARN"
        else:
            final_status = "OK"

        error_repos = [r for r in repo_results if r.get("status") == "INTERRUPTED"]
        error_msg = ""
        if error_repos:
            error_msg = f"部分仓库扫描中断: {', '.join(r['repo_id'] for r in error_repos)}"

        final_result = {
            "task": task_name,
            "status": final_status,
            "type": "DATA_ANOMALY" if final_status != "OK" else "OK",
            "summary": f"扫描了 {len(repos_to_scan)} 个仓库，发现 {total_high} HIGH / {total_medium} MEDIUM / {total_low} LOW",
            "reason_short": error_msg or f"共 {len(all_findings)} 个安全问题",
            "owasp_source": repo_results[0].get("owasp_source", "official") if repo_results else "unknown",
            "owasp_version": repo_results[0].get("owasp_version", "") if repo_results else "",
            "details": {
                "scanned_repos": len(repos_to_scan),
                "scanned_items": 10,
                "findings_count": len(all_findings),
                "high_count": total_high,
                "medium_count": total_medium,
                "low_count": total_low,
                "report_path": None,
                "findings": all_findings,
                "sensitive_info": all_sensitive_info,
                "repo_reports": repo_reports,
            },
            "error": {"message": error_msg, "evidence": {}} if error_msg else {"message": "", "evidence": {}},
        }

    duration = 0
    send_alert(task_name, 0, "success", duration, 0, json.dumps(final_result), task_timestamp, task_conf, final_result)


# ============================================================
# 解析 SUMMARY_JSON
# ============================================================
def parse_summary_json(result_text: str) -> dict:
    if not result_text:
        return {}

    marker = "SUMMARY_JSON:"
    if marker in result_text:
        try:
            parts = result_text.rsplit(marker, 1)
            json_str = parts[1].strip()
            if "}" in json_str:
                json_str = json_str[:json_str.rfind("}")+1]
            data = json.loads(json_str)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"SUMMARY_JSON 解析失败: {e}")

    return {}


# ============================================================
# 解析报告路径
# ============================================================
def parse_report_path(result_text: str, task_name: str) -> str:
    if not result_text:
        return None

    patterns = [
        task_name + r"_(\d{8}_\d{6})\.pdf",
        task_name + r"_(\d{8}_\d{6})\.html",
        r"PDF报告[:：]\s*[`']?([^\s`']+\.pdf)",
        r"报告[:：]\s*[`']?([^\s`']+\.(?:pdf|html))",
    ]

    for pattern in patterns:
        match = re.search(pattern, result_text)
        if match:
            timestamp = match.group(1) if match.groups() else None
            if timestamp:
                reports_dir = LOG_DIR / "reports"
                for ext in ["pdf", "html"]:
                    candidate = reports_dir / f"{task_name}_{timestamp}.{ext}"
                    if candidate.exists():
                        return str(candidate)
    return None


# ============================================================
# 上传报告到企微
# ============================================================
def upload_report_to_wecom(webhook: str, report_path: str, task_name: str):
    try:
        with open(report_path, "rb") as f:
            files = {"file": (os.path.basename(report_path), f, "application/octet-stream")}
            data = {"filename": os.path.basename(report_path), "title": f"{task_name} 安全扫描报告"}
            resp = requests.post(
                webhook.replace("/send?", "/upload_media?"),
                files=files,
                data=data,
                timeout=30,
            )
        result = resp.json()
        if result.get("errcode") == 0:
            media_id = result.get("media_id")
            file_msg = {"msgtype": "file", "file": {"media_id": media_id}}
            requests.post(webhook, json=file_msg, timeout=10)
            logger.info(f"报告已上传企微: {report_path}")
        else:
            logger.warning(f"报告上传企微失败: {result.get('errmsg')}")
    except Exception as e:
        logger.error(f"报告上传失败: {e}")


# ============================================================
# 告警（企微 webhook 通知）
# ============================================================
def send_alert(task_name: str, exit_code: int, subtype: str,
               duration: int, tokens, result_text: str, timestamp: str,
               task_conf: dict = None, final_result: dict = None):
    task_conf = task_conf or {}
    webhook_env = task_conf.get("alert_webhook_env", "ALERT_WEBHOOK")

    if str(webhook_env).lower() in ["false", "none", "null", ""]:
        logger.info(f"[{task_name}] 告警已被显式禁用")
        return

    webhook = os.getenv(webhook_env)
    if not webhook:
        logger.warning(f"{webhook_env} 未配置，跳过告警")
        return

    if final_result is None:
        final_result = parse_summary_json(result_text)

    status = final_result.get("status", "UNKNOWN")
    summary_type = final_result.get("type", "UNKNOWN")
    summary_msg = final_result.get("summary", "") or final_result.get("reason_short", "")
    details = final_result.get("details", {})
    findings_count = details.get("findings_count", 0)
    high_count = details.get("high_count", 0)
    medium_count = details.get("medium_count", 0)
    low_count = details.get("low_count", 0)
    scanned_repos = details.get("scanned_repos", 1)

    meta = f"扫描仓库数: {scanned_repos}"

    if status == "OK":
        icon, head = "✅", "安全扫描正常"
        body = f"结果: 无异常发现\n{meta}"
    elif status == "WARN":
        icon, head = "🟡", "安全扫描异常"
        body = f"发现 {findings_count} 个问题 (HIGH:{high_count} MEDIUM:{medium_count} LOW:{low_count})\n{summary_msg}\n{meta}"
    elif status == "ERROR":
        icon, head = "🔴", "安全扫描严重"
        body = f"发现 {findings_count} 个问题 (HIGH:{high_count} MEDIUM:{medium_count} LOW:{low_count})\n{summary_msg}\n{meta}"
    elif status == "INTERRUPTED":
        icon, head = "⚠️", "扫描中断"
        body = f"原因: {summary_type}\n{summary_msg}\n日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"
    else:
        icon, head = "❓", "结果未知"
        body = f"原因: 解析失败\n日志: claude/logs/{task_name}_{timestamp}.log\n{meta}"

    content = f"【{task_name}】{icon} {head}\n{body}"

    try:
        # 1. 先发送文本消息
        requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        logger.info(f"告警已发送: {task_name} {head}")

        # 2. 发送各仓库报告（紧跟文本消息之后）
        repo_reports = details.get("repo_reports", [])
        if repo_reports:
            for repo_report in repo_reports:
                repo_name = repo_report.get("repo_name", repo_report.get("repo_id", ""))
                repo_report_path = repo_report.get("report_path")
                if repo_report_path and os.path.exists(repo_report_path):
                    # 发送仓库报告前先发一条分隔文本
                    sep_content = f"--- {repo_name} 报告 ---"
                    requests.post(
                        webhook,
                        json={"msgtype": "text", "text": {"content": sep_content}},
                        timeout=10,
                    )
                    upload_report_to_wecom(webhook, repo_report_path, f"{task_name}_{repo_name}")
        else:
            # 单报告模式（兼容旧逻辑）
            report_path = details.get("report_path")
            if not report_path:
                report_path = parse_report_path(result_text, task_name)
            if report_path and os.path.exists(report_path):
                upload_report_to_wecom(webhook, report_path, task_name)

    except Exception as e:
        logger.error(f"告警发送失败: {e}")


# ============================================================
# 日志清理
# ============================================================
sys.path.append(str(ROOT_DIR))
from cleanup_logs import cleanup_all_logs


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
    args = parser.parse_args()

    if args.task:
        run_task(args.task, args.repo, args.branch)
        return

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    tasks_dir = ROOT_DIR / "tasks"
    defaults = CONFIG.get("global_defaults", {})

    for task_file in tasks_dir.glob("*.md"):
        if task_file.name.startswith("_"):
            continue

        task_name = task_file.stem
        task_conf = load_task_config(task_name)

        schedule_expr = task_conf.get("schedule", defaults.get("schedule", "0 9 * * 1-5"))

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
                name=task_name,
                replace_existing=True
            )
            logger.info(f"已注册定时任务: {task_name} | schedule: {schedule_expr}")
        except Exception as e:
            logger.error(f"任务 {task_name} 的 Cron 表达式解析失败 [{schedule_expr}]: {e}")

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
