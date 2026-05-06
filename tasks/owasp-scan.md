---
schedule: "0 3 * * 1"
budget: 2.0
max_turns: 30
timeout: 1800
db_host: false
target_type: git_repositories
target_config: repositories.yaml
allowed_tools:
  - "Read"
  - "Glob"
  - "Grep"
  - "Bash(git clone *)"
  - "Bash(git fetch *)"
  - "Bash(git checkout *)"
  - "Bash(git log *)"
  - "Bash(git rev-parse *)"
  - "Bash(curl *)"
  - "Bash(grep *)"
  - "Bash(find *)"
  - "Bash(cat *)"
  - "Bash(head *)"
  - "Bash(tail *)"
  - "Bash(wc *)"
  - "Bash(python *)"
  - "Bash(python3 *)"
---

# 任务：OWASP Top 10 安全扫描 + 敏感信息检测

## 扫描目标

本任务通过读取 `repositories.yaml` 中注册的 Git 仓库，对每个仓库执行安全扫描。

## 环境变量（由调度器注入）

- `SCAN_REPO_ID` - 仓库唯一标识
- `SCAN_REPO_NAME` - 仓库展示名称
- `SCAN_REPO_URL` - 仓库地址
- `SCAN_BRANCH` - 扫描分支
- `SCAN_COMMIT` - 当前 commit hash
- `SCAN_CHECKOUT_PATH` - 代码 checkout 目录路径

## 阶段一：动态获取 OWASP Top 10

1. 优先访问 OWASP 官方 2025 页面：
   https://owasp.org/Top10/2025/

2. 提取当前 OWASP Top 10 列表，包括编号、名称、风险描述

3. 如果官方页面不可访问，使用备用源：
   https://owasp.org/Top10/2021/

4. 如果备用源也不可用，使用内置知识 fallback

**来源标记规则**：
- 从 2025 页面获取成功：`owasp_source: "official_2025"`, `owasp_version: "2025"`
- 从 2021 页面获取成功：`owasp_source: "official_2021"`, `owasp_version: "2021"`
- 使用内置知识 fallback：`owasp_source: "builtin_fallback"`, `owasp_version: "fallback"`

## 阶段二：逐项扫描代码库

扫描目录：`${SCAN_CHECKOUT_PATH}`

排除目录：`node_modules`, `.git`, `__pycache__`, `venv`, `.venv`, `dist`, `build`, `target`, `.idea`, `.vscode`

**重要**：阶段一获取的 OWASP Top 10 列表是动态的（可能是 2025、2021 或未来版本），每个风险项的具体名称和描述以网页内容为准。扫描时需根据阶段一获取的实际列表逐项扫描。

### 通用扫描覆盖点（适用于所有版本）

**访问控制类**：
- 路由或接口缺少认证/鉴权
- IDOR 风险（直接对象引用）
- 管理接口无权限校验
- 未授权访问敏感 API

**加密与密钥类**：
- 硬编码密钥/密码
- 弱加密算法（DES、MD5 等）
- 明文密码存储
- HTTP 明文传输敏感数据

**注入类**：
- SQL 字符串拼接
- 命令注入（Process.Start、eval 等）
- NoSQL/LDAP/XPath 注入模式

**安全配置类**：
- debug=true / DEBUG=True
- 默认密码
- CORS 过宽配置
- 错误栈信息泄露
- 不安全默认配置

**组件与依赖类**：
- requirements.txt / package.json / pom.xml / go.mod
- 已知漏洞组件版本
- 过时依赖库

**认证与会话类**：
- 弱密码策略
- JWT/Session 配置问题
- 凭证明文传输
- 认证绕过风险

**数据完整性类**：
- 不安全反序列化
- 第三方资源未校验完整性
- 不安全 CI/CD 配置

**日志与监控类**：
- 敏感操作缺少审计日志
- 日志中输出密码、token、secret
- 安全事件未记录

**服务端请求类**：
- 用户输入参与 URL 请求
- 缺少 URL 白名单
- SSRF 漏洞模式

## 阶段三：全局敏感信息扫描

必须扫描：
1. 硬编码密码
2. API Key / Secret Key
3. Access Token / Auth Token
4. Private Key
5. AWS Access Key
6. 数据库连接串
7. webhook URL
8. JWT Secret

**脱敏规则**：原始值 `abcd1234567890`，输出 `abcd****7890`。完整密钥、token、密码不得写入报告或日志。

## 阶段四：生成安全扫描报告

### 触发条件
- 无问题时：`report_path = null`
- 有问题时：生成报告，优先 PDF，fallback HTML

### 报告保存位置
`claude/logs/reports/owasp-scan_YYYYMMDD_HHMMSS.pdf`
或
`claude/logs/reports/owasp-scan_YYYYMMDD_HHMMSS.html`

### 报告内容（中文）
1. 报告标题
2. 扫描时间
3. 目标仓库信息
4. OWASP 来源
5. 发现汇总（HIGH/MEDIUM/LOW 统计）
6. 详细发现列表（含脱敏）
7. 敏感信息发现列表（必须脱敏）
8. 扫描统计

## 输出要求

**最后一行必须严格输出 SUMMARY_JSON，不得输出其他解释性文字：**

```
SUMMARY_JSON:{"task":"owasp-scan","status":"OK","type":"OK","summary":"无异常发现","reason_short":"","owasp_source":"official_2025","owasp_version":"2025","details":{"scanned_items":10,"findings_count":0,"high_count":0,"medium_count":0,"low_count":0,"report_path":null,"findings":[],"sensitive_info":[]},"error":{"message":"","evidence":{}}}
```

### 状态规则
- 无任何发现：`status = "OK"`, `type = "OK"`, `report_path = null`
- 有 LOW/MEDIUM：`status = "WARN"`, `type = "DATA_ANOMALY"`, 生成报告
- 有 HIGH 或敏感信息泄露：`status = "ERROR"`, `type = "DATA_ANOMALY"`, 生成报告
- 扫描中断：`status = "INTERRUPTED"`, `type` 按原因填写

### findings 单项结构

**重要**：`owasp_id` 和 `owasp_name` 必须使用阶段一实际解析出的 OWASP 条目编号和名称，不得硬编码 2021 版本标题。

例如阶段一解析到 2025 版本：
- `owasp_id`: "A01:2025"
- `owasp_name`: "Broken Access Control"

例如阶段一解析到 2021 版本：
- `owasp_id`: "A01:2021"
- `owasp_name": "Broken Access Control"

```json
{"owasp_id":"A01:2025","owasp_name":"Broken Access Control","severity":"HIGH","file":"path/to/file.py","line":42,"pattern":"脱敏后的代码片段","description":"问题描述","recommendation":"修复建议"}
```

### sensitive_info 单项结构
```json
{"type":"api_key","severity":"HIGH","file":"path/to/file.py","line":12,"masked_value":"abcd****7890","description":"疑似硬编码 API Key","recommendation":"移入环境变量或密钥管理服务"}
```
