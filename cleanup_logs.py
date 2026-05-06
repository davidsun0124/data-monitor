import time
import logging
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s"
)
logger = logging.getLogger("cleanup")

def cleanup_all_logs(keep_days: int = 90):
    """
    清理过期文件：
    1. claude/logs/ 下的 .log（保留 scheduler.log）
    2. claude/logs/reports/ 下的 .pdf / .html
    3. scratch/security-scan/ 下的临时 checkout 目录
    """
    root_dir = Path(__file__).resolve().parent
    cutoff = time.time() - keep_days * 86400
    count = 0

    # 1. 清理所有一级子目录下 logs 文件夹的 .log 文件
    for log_dir in root_dir.glob("*/logs"):
        if not log_dir.is_dir():
            continue

        logger.info(f"检查日志目录: {log_dir}")

        for f in log_dir.glob("*.log"):
            if f.name == "scheduler.log":
                continue
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    count += 1
                except Exception as e:
                    logger.error(f"删除失败 {f}: {e}")

        # 2. 清理 logs/reports/ 下的报告文件
        reports_dir = log_dir / "reports"
        if reports_dir.is_dir():
            for f in reports_dir.glob("*.pdf"):
                if f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                        count += 1
                    except Exception as e:
                        logger.error(f"删除失败 {f}: {e}")
            for f in reports_dir.glob("*.html"):
                if f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                        count += 1
                    except Exception as e:
                        logger.error(f"删除失败 {f}: {e}")

    # 3. 清理 scratch/security-scan/ 下的临时 checkout 目录
    scratch_scan_dir = root_dir / "scratch" / "security-scan"
    if scratch_scan_dir.is_dir():
        logger.info(f"检查临时目录: {scratch_scan_dir}")
        for repo_dir in scratch_scan_dir.iterdir():
            if not repo_dir.is_dir():
                continue
            for scan_dir in repo_dir.iterdir():
                if scan_dir.name.startswith("owasp-scan_") and scan_dir.is_dir():
                    if scan_dir.stat().st_mtime < cutoff:
                        try:
                            import shutil
                            shutil.rmtree(scan_dir)
                            count += 1
                        except Exception as e:
                            logger.error(f"删除失败 {scan_dir}: {e}")

    if count > 0:
        logger.info(f"清理完成，共删除了 {count} 个过期文件")
    else:
        logger.info("清理完成，没有需要删除的过期文件")

if __name__ == "__main__":
    from apscheduler.schedulers.blocking import BlockingScheduler

    logger.info("启动独立的日志清理调度服务...")
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    
    # 每3个月（1,4,7,10月的1号）凌晨 2 点执行清理
    scheduler.add_job(
        cleanup_all_logs,
        trigger="cron",
        month="1,4,7,10",
        day=1,
        hour=2,
        minute=0,
        id="cleanup_all_logs",
    )
    
    # 启动时先执行一次（可选）
    cleanup_all_logs()
    
    logger.info("日志清理服务已在后台运行，每三个月的1号凌晨 2 点自动执行。按 Ctrl+C 退出。")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("日志清理服务已停止")
