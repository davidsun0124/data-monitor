import os
import sys
from pathlib import Path

# 将 claude 目录加入路径
sys.path.append(str(Path(__file__).parent.parent / 'claude'))

from scheduler import send_alert

def test_push():
    print("🚀 正在发起企微告警测试...")
    
    # 模拟数据
    task_name = "测试任务_连接验证"
    exit_code = 0
    subtype = "success"
    duration = 5
    tokens = 1024
    timestamp = "2024-04-24_TEST"
    
    # 模拟一个符合格式的 FAIL 结果
    result_text = """
一些中间过程日志...
SUMMARY_JSON:{"status": "FAIL", "level": "CRITICAL", "brief": "测试：发现 3 条异常数据", "anomaly_types": ["逻辑校验失败"], "top5": ["订单号: 10001", "订单号: 10002", "订单号: 10003"]}
"""
    
    # 模拟任务配置
    task_conf = {"alert_webhook_env": "ALERT_WEBHOOK"}
    
    try:
        send_alert(
            task_name=task_name,
            exit_code=exit_code,
            subtype=subtype,
            duration=duration,
            tokens=tokens,
            result_text=result_text,
            timestamp=timestamp,
            task_conf=task_conf
        )
        print("\n✅ 调用完成。请检查企微群聊是否有消息弹出。")
        print("如果没收到，请检查：")
        print("1. .env 文件中的 ALERT_WEBHOOK 是否正确")
        print("2. 网络是否能访问 qyapi.weixin.qq.com")
    except Exception as e:
        print(f"\n❌ 发送失败: {e}")

if __name__ == "__main__":
    test_push()
