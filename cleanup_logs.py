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
    通用日志清理逻辑：
    遍历项目根目录下所有执行器的 logs 文件夹（例如 claude/logs, qoderwork/logs），
    删除修改时间超过指定天数的 .log 文件。
    """
    root_dir = Path(__file__).resolve().parent
    cutoff = time.time() - keep_days * 86400
    count = 0
    
    # 查找所有一级子目录下的 logs 文件夹
    for log_dir in root_dir.glob("*/logs"):
        if not log_dir.is_dir():
            continue
            
        logger.info(f"检查日志目录: {log_dir}")
        for f in log_dir.glob("*.log"):
            # 跳过调度器的主日志等常驻文件
            if f.name == "scheduler.log":
                continue
                
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    count += 1
                except Exception as e:
                    logger.error(f"删除失败 {f}: {e}")
                    
    if count > 0:
        logger.info(f"清理完成，共删除了 {count} 个过期日志文件")
    else:
        logger.info("清理完成，没有需要删除的过期日志")

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
