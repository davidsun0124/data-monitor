---
schedule: "manual"
db_host: ""
---

# 任务：系统运行环境自检

## 巡检背景
检查数据监控系统当前的运行环境，确保日志目录和配置正常。

## 巡检步骤
1. 列出当前目录下 `claude/logs` 目录中的文件数量。
2. 确认 `config.yaml` 是否存在。
3. 给出简单的健康度评价。

## 数据库约束
(本任务暂不涉及数据库查询)

## 告警逻辑
- 必须在输出的最后，包含一行 `SUMMARY_JSON:` 前缀的内容，格式如下：
  SUMMARY_JSON:{"status": "SUCCESS", "level": "INFO", "brief": "系统环境自检通过", "anomaly_types": [], "top5": []}
- 如果日志目录不存在，status 为 FAIL，level 为 CRITICAL。
