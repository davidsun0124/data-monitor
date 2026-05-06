"""
统一任务执行入口 (Task Runner)

所有任务（普通数据监控、Git 仓库类任务）都通过 run_task() 执行。
外部脚本、AI Agent、Webhook、企微机器人、CI/CD 都调用同一个入口。

职责：
1. 读取 config.yaml 的 global_defaults
2. 读取 tasks/{task}.md YAML Frontmatter
3. 合并配置（支持 false 显式关闭）
4. 注入 variables
5. 根据任务类型执行任务
6. 解析 SUMMARY_JSON
7. 统一写日志
8. 统一发送企微通知
9. 返回 dict 结果
"""

import subprocess
import os
import sys
import json
import time
import logging
import requests
import yaml
import re
import shutil
from datetime import datetime
from pathlib import Path
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
REPORTS_DIR = LOG_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("task_runner")

# ============================================================
# 辅助函数
# ============================================================
def _is_disabled(val):
    """判断值是否表示禁用（false/none/null/空字符串）"""
    return str(val).lower() in ["false", "none", "null", ""]


def _mask_sensitive(value: str, show_chars: int = 4) -> str:
    """脱敏敏感信息，只显示前 show_chars 位"""
    if not value or len(value) <= show_chars:
        return "****"
    return value[:show_chars] + "****"


def _desensitize_log(text: str) -> str:
    """在日志中脱敏敏感信息"""
    # 脱敏 GitLab token
    text = re.sub(
        r"https://oauth2:[^@]+@",
        "https://oauth2:****@",
        text
    )
    # 脱敏可能的 API key / token
    text = re.sub(
        r"(token|key|secret|password|passwd|pwd)=[a-zA-Z0-9+/]{10,}",
        lambda m: f"{m.group(1)}={_mask_sensitive(m.group(2))}",
        text,
        flags=re.IGNORECASE
    )
    return text


# ============================================================
# 配置加载与合并
# ============================================================
def load_global_defaults() -> dict:
    """加载 config.yaml 的 global_defaults"""
    return CONFIG.get("global_defaults", {})


def load_task_frontmatter(task_name: str) -> dict:
    """读取 tasks/{task}.md 的 YAML Frontmatter"""
    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    if not task_file.exists():
        return {}

    content = task_file.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}

    try:
        end_pos = content.find("---", 3)
        if end_pos == -1:
            return {}
        frontmatter_text = content[3:end_pos]
        frontmatter = yaml.safe_load(frontmatter_text)
        if isinstance(frontmatter, dict):
            return frontmatter
    except Exception as e:
        logger.warning(f"解析 {task_name} Frontmatter 失败: {e}")

    return {}


def merge_config(task_name: str, external_variables: dict = None) -> dict:
    """
    合并配置：
    1. config.yaml global_defaults 作为兜底
    2. 任务 md Frontmatter 覆盖（false 表示显式关闭）
    3. external_variables 覆盖所有

    返回合并后的配置字典
    """
    defaults = load_global_defaults()

    # 基础兜底配置
    conf = {
        "schedule": defaults.get("schedule", "0 9 * * 1-5"),
        "budget": defaults.get("budget", 0.50),
        "max_turns": defaults.get("max_turns", 15),
        "timeout": defaults.get("timeout", 600),
        "default_db_host": defaults.get("default_db_host", "EOS_DB_HOST"),
        "alert_webhook_env": defaults.get("alert_webhook_env", "ALERT_WEBHOOK"),
    }

    # 读取任务 md Frontmatter
    frontmatter = load_task_frontmatter(task_name)
    if isinstance(frontmatter, dict):
        for k, v in frontmatter.items():
            if v is False:
                # 显式写 false 表示关闭该配置，不使用默认值
                conf[k] = False
            else:
                conf[k] = v

    # 外部传入 variables 优先级最高
    if external_variables and isinstance(external_variables, dict):
        for k, v in external_variables.items():
            conf[k] = v

    return conf


def get_task_prompt(task_name: str) -> str:
    """读取任务 md 的正文（去掉 Frontmatter）"""
    task_file = ROOT_DIR / "tasks" / f"{task_name}.md"
    if not task_file.exists():
        return ""

    content = task_file.read_text(encoding="utf-8")
    if content.startswith("---"):
        end_pos = content.find("---", 3)
        if end_pos != -1:
            return content[end_pos+3:].strip()
    return content.strip()


# ============================================================
# Git 仓库支持
# ============================================================
def load_repositories(repositories_config: str) -> list:
    """加载 repositories.yaml"""
    config_path = ROOT_DIR / repositories_config
    if not config_path.exists():
        logger.error(f"仓库配置文件不存在: {config_path}")
        return []

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return data.get("repositories", [])


def clone_repo(repo: dict, branch: str, task_name: str, run_id: str) -> dict:
    """
    克隆 Git 仓库到本地

    clone 目录：scratch/security-scan/{repo_id}/{task_name}_{timestamp}/
    """
    repo_id = repo["id"]
    repo_url = repo["repo_url"]
    token = os.getenv(repo.get("token_env", "GITLAB_TOKEN"))

    # 构建 clone 路径
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clone_base = ROOT_DIR / repo.get("clone_base", "scratch/security-scan")
    checkout_path = clone_base / repo_id / f"{task_name}_{timestamp}"

    checkout_path.parent.mkdir(parents=True, exist_ok=True)

    # 如果已存在，先删除
    if checkout_path.exists():
        shutil.rmtree(checkout_path)

    # 构造带 token 的 URL（仅用于需要认证的情况）
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
            return {
                "repo_id": repo_id,
                "repo_name": repo.get("name", repo_id),
                "repo_url": repo_url,
                "success": False,
                "error": error_msg,
                "checkout_path": None,
                "commit": None,
                "branch": branch,
            }

        # 获取 commit hash
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
            "repo_name": repo.get("name", repo_id),
            "repo_url": repo_url,
            "success": True,
            "checkout_path": str(checkout_path),
            "commit": commit,
            "branch": branch,
        }

    except subprocess.TimeoutExpired:
        return {
            "repo_id": repo_id,
            "repo_name": repo.get("name", repo_id),
            "repo_url": repo_url,
            "success": False,
            "error": "克隆超时（>300s）",
            "checkout_path": None,
            "commit": None,
            "branch": branch,
        }
    except Exception as e:
        return {
            "repo_id": repo_id,
            "repo_name": repo.get("name", repo_id),
            "repo_url": repo_url,
            "success": False,
            "error": str(e),
            "checkout_path": None,
            "commit": None,
            "branch": branch,
        }


# ============================================================
# 执行任务
# ============================================================
def execute_task_via_claude(prompt: str, conf: dict, run_id: str) -> tuple:
    """
    通过 Claude CLI 执行任务

    返回：(exit_code, raw_output)
    """
    budget = conf.get("budget", 0.5)
    max_turns = conf.get("max_turns", 15)
    task_timeout = conf.get("timeout", 600)

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

    return exit_code, raw_output, duration


def parse_summary_json(result_text: str) -> dict:
    """解析 SUMMARY_JSON"""
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


def parse_report_path(result_text: str, task_name: str) -> str:
    """从输出中解析报告路径"""
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
                for ext in ["pdf", "html"]:
                    candidate = REPORTS_DIR / f"{task_name}_{timestamp}.{ext}"
                    if candidate.exists():
                        return str(candidate)
    return None


def write_log(run_id: str, task_name: str, raw_output: str, exit_code: int,
              subtype: str, duration: int, tokens: int, cost: str):
    """写任务执行日志"""
    log_file = LOG_DIR / f"{run_id}.log"

    with open(log_file, "w", encoding="utf-8") as f:
        f.write("========================================\n")
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行: {task_name}\n")
        f.write("========================================\n")
        f.write(_desensitize_log(raw_output) + "\n")
        f.write("\n----------------------------------------\n")
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 执行完成\n")
        f.write(f"  退出码 : {exit_code}\n")
        f.write(f"  子类型 : {subtype}\n")
        f.write(f"  耗时   : {duration}s\n")
        f.write(f"  Tokens : {tokens}\n")
        f.write("----------------------------------------\n")

    return str(log_file)


def upload_report_to_wecom(webhook: str, report_path: str, task_name: str):
    """上传报告到企微"""
    try:
        with open(report_path, "rb") as f:
            files = {"file": (os.path.basename(report_path), f, "application/octet-stream")}
            data = {"filename": os.path.basename(report_path), "title": f"{task_name} 扫描报告"}
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


def send_wecom_notification(task_name: str, run_id: str, conf: dict, final_result: dict,
                           repo_results: list = None):
    """
    发送企微通知

    通知规则：
    - alert_webhook_env: false 时不发送
    - 正常发送文本消息
    - Git 仓库任务发送各仓库报告
    """
    webhook_env = conf.get("alert_webhook_env", "ALERT_WEBHOOK")

    if _is_disabled(webhook_env):
        logger.info(f"[{task_name}] 告警已被显式禁用")
        return

    webhook = os.getenv(webhook_env)
    if not webhook:
        logger.warning(f"{webhook_env} 未配置，跳过告警")
        return

    status = final_result.get("status", "UNKNOWN")
    summary_type = final_result.get("type", "UNKNOWN")
    summary_msg = final_result.get("summary", "") or final_result.get("reason_short", "")
    details = final_result.get("details", {})
    findings_count = details.get("findings_count", 0)
    high_count = details.get("high_count", 0)
    medium_count = details.get("medium_count", 0)
    low_count = details.get("low_count", 0)
    scanned_repos = details.get("scanned_repos", 1) if repo_results is None else len(repo_results)

    meta = f"任务: {task_name} | run_id: {run_id}"
    if repo_results is not None:
        meta = f"扫描仓库数: {scanned_repos}"

    if status == "OK":
        icon, head = "✅", "任务执行正常"
        body = f"结果: 无异常发现\n{meta}"
    elif status == "WARN":
        icon, head = "🟡", "任务执行异常"
        body = f"发现 {findings_count} 个问题 (HIGH:{high_count} MEDIUM:{medium_count} LOW:{low_count})\n{summary_msg}\n{meta}"
    elif status == "ERROR":
        icon, head = "🔴", "任务执行严重"
        body = f"发现 {findings_count} 个问题 (HIGH:{high_count} MEDIUM:{medium_count} LOW:{low_count})\n{summary_msg}\n{meta}"
    elif status == "INTERRUPTED":
        icon, head = "⚠️", "任务中断"
        body = f"原因: {summary_type}\n{summary_msg}\n日志: claude/logs/{run_id}.log\n{meta}"
    else:
        icon, head = "❓", "结果未知"
        body = f"原因: 解析失败\n日志: claude/logs/{run_id}.log\n{meta}"

    content = f"【{task_name}】{icon} {head}\n{body}"

    try:
        # 1. 发送文本消息
        requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        logger.info(f"告警已发送: {task_name} {head}")

        # 2. 发送各仓库报告（Git 仓库任务）
        if repo_results:
            for repo_result in repo_results:
                repo_name = repo_result.get("repo_name", repo_result.get("repo_id", ""))
                report_path = repo_result.get("report_path")
                if report_path and os.path.exists(report_path):
                    # 先发分隔文本
                    sep_content = f"--- {repo_name} 报告 ---"
                    requests.post(
                        webhook,
                        json={"msgtype": "text", "text": {"content": sep_content}},
                        timeout=10,
                    )
                    upload_report_to_wecom(webhook, report_path, f"{task_name}_{repo_name}")
        else:
            # 单报告模式
            report_path = details.get("report_path")
            if not report_path:
                report_path = parse_report_path(
                    final_result.get("raw_output", ""), task_name
                )
            if report_path and os.path.exists(report_path):
                upload_report_to_wecom(webhook, report_path, task_name)

    except Exception as e:
        logger.error(f"告警发送失败: {e}")


def build_summary_result(task_name: str, run_id: str, trigger_source: str,
                         status: str, summary_type: str, summary: str,
                         reason_short: str, details: dict,
                         error_msg: str, error_evidence: dict,
                         repo_results: list = None) -> dict:
    """构建统一格式的结果字典"""
    result = {
        "task": task_name,
        "run_id": run_id,
        "trigger_source": trigger_source,
        "status": status,
        "type": summary_type,
        "summary": summary,
        "reason_short": reason_short,
        "details": details,
        "error": {
            "message": error_msg,
            "evidence": error_evidence,
        }
    }

    if repo_results is not None:
        result["repositories"] = repo_results

    return result


# ============================================================
# 普通任务执行（无 Git 仓库）
# ============================================================
def run_local_task(task_name: str, conf: dict, run_id: str,
                   trigger_source: str, notify: bool) -> dict:
    """
    执行普通数据监控任务

    流程：
    1. 读取任务 prompt
    2. 注入数据库连接信息
    3. 注入外部 variables
    4. 调用 Claude 执行
    5. 解析 SUMMARY_JSON
    6. 写日志
    7. 发送企微通知
    8. 返回结果
    """
    logger.info(f"[{run_id}] 开始执行普通任务: {task_name}")

    prompt = get_task_prompt(task_name)
    if not prompt:
        return build_summary_result(
            task_name=task_name,
            run_id=run_id,
            trigger_source=trigger_source,
            status="ERROR",
            summary_type="TASK_NOT_FOUND",
            summary="",
            reason_short=f"任务文件不存在: tasks/{task_name}.md",
            details={},
            error_msg=f"任务文件不存在: tasks/{task_name}.md",
            error_evidence={},
        )

    # 注入环境变量
    for key, val in os.environ.items():
        prompt = prompt.replace(f"${{{key}}}", val)

    # 注入数据库连接信息（如果配置了 db_host）
    db_host_var = conf.get("db_host")
    if db_host_var is None:
        db_host_var = conf.get("default_db_host")
    if _is_disabled(db_host_var):
        db_host_var = None

    if db_host_var:
        db_hint = f"【连接信息】数据库 Host 变量为 `${{{db_host_var}}}`。请依据此变量名及其前缀，在环境中查找对应的 PORT, USER, PASS, NAME 变量进行连接。"
        if "连接信息" not in prompt:
            prompt = f"{db_hint}\n\n{prompt}"

    # 执行
    exit_code, raw_output, duration = execute_task_via_claude(prompt, conf, run_id)

    # 解析输出
    cost, tokens, subtype = "N/A", "N/A", "unknown"
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
        result_text = raw_output

    # 解析 SUMMARY_JSON
    summary_data = parse_summary_json(raw_output)
    if not summary_data:
        if exit_code == -1:
            summary_data = {"status": "ERROR", "type": "TIMEOUT", "summary": f"执行超时（>{conf.get('timeout', 600)}s）"}
        elif exit_code == -2:
            summary_data = {"status": "ERROR", "type": "EXCEPTION", "summary": "执行异常"}
        elif exit_code != 0:
            summary_data = {"status": "ERROR", "type": "AI_OUTPUT_INVALID_JSON", "summary": "AI 输出无效 JSON"}
        else:
            summary_data = {"status": "OK", "type": "OK", "summary": "执行完成"}

    # 写日志
    log_path = write_log(run_id, task_name, raw_output, exit_code, subtype, duration, tokens, cost)
    summary_data["details"] = summary_data.get("details", {})
    summary_data["details"]["log_path"] = log_path
    summary_data["details"]["duration"] = duration
    summary_data["details"]["tokens"] = tokens
    summary_data["details"]["cost"] = cost
    summary_data["details"]["exit_code"] = exit_code

    # 构建结果
    final_result = build_summary_result(
        task_name=task_name,
        run_id=run_id,
        trigger_source=trigger_source,
        status=summary_data.get("status", "ERROR"),
        summary_type=summary_data.get("type", "UNKNOWN"),
        summary=summary_data.get("summary", ""),
        reason_short=summary_data.get("reason_short", ""),
        details=summary_data.get("details", {}),
        error_msg=summary_data.get("error", {}).get("message", "") if isinstance(summary_data.get("error"), dict) else "",
        error_evidence=summary_data.get("error", {}).get("evidence", {}) if isinstance(summary_data.get("error"), dict) else {},
    )

    # 发送通知
    if notify and not _is_disabled(conf.get("alert_webhook_env")):
        send_wecom_notification(task_name, run_id, conf, final_result)

    logger.info(f"[{run_id}] 任务完成: status={final_result['status']}")
    return final_result


# ============================================================
# Git 仓库任务执行
# ============================================================
def run_git_task(task_name: str, conf: dict, run_id: str,
                 trigger_source: str, notify: bool,
                 specific_repo: str = None,
                 override_branch: str = None) -> dict:
    """
    执行 Git 仓库类任务

    流程：
    1. 读取 repositories.yaml
    2. 筛选 enabled=true 且 tasks 包含当前任务
    3. 如果指定了 repo，只扫描该仓库
    4. 如果指定了 branch，临时覆盖分支
    5. 对每个仓库执行：clone -> 注入变量 -> 执行任务 -> 汇总结果
    """
    logger.info(f"[{run_id}] 开始执行 Git 仓库任务: {task_name}")

    target_config = conf.get("target_config", "repositories.yaml")
    all_repos = load_repositories(target_config)

    # 筛选需要扫描的仓库
    repos_to_scan = []
    for repo in all_repos:
        if not repo.get("enabled", False):
            continue
        if specific_repo and repo["id"] != specific_repo:
            continue
        if task_name not in repo.get("tasks", []):
            continue
        repos_to_scan.append(repo)

    if not repos_to_scan:
        logger.warning(f"没有找到需要扫描的仓库: task={task_name}, repo={specific_repo}")
        return build_summary_result(
            task_name=task_name,
            run_id=run_id,
            trigger_source=trigger_source,
            status="ERROR",
            summary_type="NO_REPOS",
            summary="",
            reason_short=f"没有找到匹配的仓库: task={task_name}, repo={specific_repo}",
            details={"scanned_repos": 0},
            error_msg="没有找到需要扫描的仓库",
            error_evidence={"task": task_name, "repo": specific_repo},
        )

    # 每个仓库执行任务
    repo_results = []
    all_findings = []
    all_sensitive_info = []
    total_high = 0
    total_medium = 0
    total_low = 0
    interrupted_repos = []

    for repo in repos_to_scan:
        repo_id = repo["id"]
        branch = override_branch or repo.get("default_branch", "main")

        # Clone 仓库
        clone_result = clone_repo(repo, branch, task_name, run_id)

        if not clone_result["success"]:
            repo_results.append({
                "repo_id": repo_id,
                "repo_name": clone_result["repo_name"],
                "repo_url": clone_result["repo_url"],
                "branch": branch,
                "commit": None,
                "clone_path": None,
                "status": "INTERRUPTED",
                "error": clone_result["error"],
                "findings": [],
                "sensitive_info": [],
                "report_path": None,
            })
            interrupted_repos.append(repo_id)
            continue

        checkout_path = clone_result["checkout_path"]
        commit = clone_result["commit"]

        # 构建 prompt 并注入变量
        prompt = get_task_prompt(task_name)
        if not prompt:
            repo_results.append({
                "repo_id": repo_id,
                "repo_name": clone_result["repo_name"],
                "repo_url": clone_result["repo_url"],
                "branch": branch,
                "commit": commit,
                "clone_path": checkout_path,
                "status": "INTERRUPTED",
                "error": f"任务文件不存在: tasks/{task_name}.md",
                "findings": [],
                "sensitive_info": [],
                "report_path": None,
            })
            interrupted_repos.append(repo_id)
            continue

        # 替换扫描相关变量
        prompt = prompt.replace("${SCAN_REPO_ID}", repo_id)
        prompt = prompt.replace("${SCAN_REPO_NAME}", clone_result["repo_name"])
        prompt = prompt.replace("${SCAN_REPO_URL}", clone_result["repo_url"])
        prompt = prompt.replace("${SCAN_BRANCH}", branch)
        prompt = prompt.replace("${SCAN_COMMIT}", commit)
        prompt = prompt.replace("${SCAN_CHECKOUT_PATH}", checkout_path)

        # 注入环境变量
        for key, val in os.environ.items():
            prompt = prompt.replace(f"${{{key}}}", val)

        # 执行
        exit_code, raw_output, duration = execute_task_via_claude(prompt, conf, run_id)

        # 解析输出
        try:
            data = json.loads(raw_output.strip())
            subtype = data.get("subtype", "unknown")
        except Exception:
            subtype = "unknown"

        # 解析 SUMMARY_JSON
        summary_data = parse_summary_json(raw_output)
        if not summary_data:
            if exit_code == -1:
                summary_data = {"status": "ERROR", "type": "TIMEOUT", "summary": f"执行超时"}
            elif exit_code == -2:
                summary_data = {"status": "ERROR", "type": "EXCEPTION", "summary": "执行异常"}
            elif exit_code != 0:
                summary_data = {"status": "ERROR", "type": "AI_OUTPUT_INVALID_JSON", "summary": "AI 输出无效 JSON"}
            else:
                summary_data = {"status": "OK", "type": "OK", "summary": "执行完成"}

        details = summary_data.get("details", {})
        findings = details.get("findings", [])
        sensitive_info = details.get("sensitive_info", [])
        report_path = details.get("report_path")

        all_findings.extend(findings)
        all_sensitive_info.extend(sensitive_info)
        total_high += details.get("high_count", 0)
        total_medium += details.get("medium_count", 0)
        total_low += details.get("low_count", 0)

        repo_results.append({
            "repo_id": repo_id,
            "repo_name": clone_result["repo_name"],
            "repo_url": clone_result["repo_url"],
            "branch": branch,
            "commit": commit,
            "clone_path": checkout_path,
            "status": summary_data.get("status", "OK"),
            "error": "",
            "findings": findings,
            "sensitive_info": sensitive_info,
            "report_path": report_path,
        })

        logger.info(f"仓库扫描完成: {repo_id}, status={summary_data.get('status', 'UNKNOWN')}")

    # 汇总结果
    has_error = any(r.get("status") == "ERROR" for r in repo_results)
    has_warn = any(r.get("status") == "WARN" for r in repo_results)

    if has_error:
        final_status = "ERROR"
    elif has_warn:
        final_status = "WARN"
    elif interrupted_repos:
        final_status = "INTERRUPTED"
    else:
        final_status = "OK"

    error_repos = [r for r in repo_results if r.get("status") == "INTERRUPTED"]
    error_msg = ""
    if error_repos:
        error_msg = f"部分仓库扫描中断: {', '.join(r['repo_id'] for r in error_repos)}"

    final_result = build_summary_result(
        task_name=task_name,
        run_id=run_id,
        trigger_source=trigger_source,
        status=final_status,
        summary_type="DATA_ANOMALY" if final_status != "OK" else "OK",
        summary=f"扫描了 {len(repos_to_scan)} 个仓库，发现 {total_high} HIGH / {total_medium} MEDIUM / {total_low} LOW",
        reason_short=error_msg or f"共 {len(all_findings)} 个安全问题",
        details={
            "scanned_repos": len(repos_to_scan),
            "scanned_items": len(all_findings) + len(all_sensitive_info),
            "findings_count": len(all_findings),
            "high_count": total_high,
            "medium_count": total_medium,
            "low_count": total_low,
            "report_path": None,
            "findings": all_findings,
            "sensitive_info": all_sensitive_info,
        },
        error_msg=error_msg,
        error_evidence={"interrupted_repos": interrupted_repos} if interrupted_repos else {},
        repo_results=repo_results,
    )

    # 发送通知
    if notify and not _is_disabled(conf.get("alert_webhook_env")):
        send_wecom_notification(task_name, run_id, conf, final_result, repo_results)

    logger.info(f"[{run_id}] Git 仓库任务完成: status={final_status}")
    return final_result


# ============================================================
# 统一入口
# ============================================================
def run_task(
    task: str,
    trigger_source: str = "manual",
    repo: str = None,
    branch: str = None,
    variables: dict = None,
    run_id: str = None,
    notify: bool = True
) -> dict:
    """
    统一任务执行入口

    参数：
        task: 任务名称（tasks/{task}.md 的 stem）
        trigger_source: 触发来源 (schedule/manual/external_script/ai_agent/gitlab_webhook/wecom_bot/api/unknown)
        repo: 只扫描指定仓库（仅 Git 仓库任务有效）
        branch: 临时覆盖分支（仅 Git 仓库任务有效）
        variables: 外部注入变量（优先级最高）
        run_id: 外部传入 run_id（如果不传则自动生成）
        notify: 是否发送企微通知（可被任务配置覆盖）

    返回：
        dict {
            "task": str,
            "run_id": str,
            "trigger_source": str,
            "status": str,
            "type": str,
            "summary": str,
            "reason_short": str,
            "details": dict,
            "error": {"message": str, "evidence": dict},
            "repositories": [list] # 仅 Git 仓库任务
        }
    """
    # 生成或使用 run_id
    if not run_id:
        run_id = f"{task}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 验证任务存在
    task_file = ROOT_DIR / "tasks" / f"{task}.md"
    if not task_file.exists():
        logger.error(f"任务文件不存在: {task_file}")
        return build_summary_result(
            task_name=task,
            run_id=run_id,
            trigger_source=trigger_source,
            status="ERROR",
            summary_type="TASK_NOT_FOUND",
            summary="",
            reason_short=f"任务文件不存在: tasks/{task}.md",
            details={},
            error_msg=f"任务文件不存在: tasks/{task}.md",
            error_evidence={},
        )

    # 合并配置
    conf = merge_config(task, variables)

    # 判断任务类型
    target_type = conf.get("target_type", "local")

    if target_type == "git_repositories":
        return run_git_task(
            task_name=task,
            conf=conf,
            run_id=run_id,
            trigger_source=trigger_source,
            notify=notify,
            specific_repo=repo,
            override_branch=branch,
        )
    else:
        return run_local_task(
            task_name=task,
            conf=conf,
            run_id=run_id,
            trigger_source=trigger_source,
            notify=notify,
        )


# ============================================================
# 便捷函数（供外部脚本调用）
# ============================================================
def run_schedule_task(task: str, **kwargs) -> dict:
    """定时调度触发"""
    return run_task(task, trigger_source="schedule", **kwargs)


def run_manual_task(task: str, **kwargs) -> dict:
    """手动触发"""
    return run_task(task, trigger_source="manual", **kwargs)


def run_external_script_task(task: str, **kwargs) -> dict:
    """外部脚本触发"""
    return run_task(task, trigger_source="external_script", **kwargs)


def run_ai_agent_task(task: str, **kwargs) -> dict:
    """AI Agent 触发"""
    return run_task(task, trigger_source="ai_agent", **kwargs)


if __name__ == "__main__":
    # 简单测试
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--notify", default="true")
    args = parser.parse_args()

    result = run_task(
        task=args.task,
        repo=args.repo,
        branch=args.branch,
        notify=args.notify.lower() == "true",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
