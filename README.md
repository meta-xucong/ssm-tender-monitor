# 澳门网页招标信息监控 (SSM Tender Monitor)

自动监控**澳门卫生局(SSM)书面询价**页面，发现符合条件的**新标书**时邮件提醒，每周五发送监控周报。

- 监控页面: https://www.ssm.gov.mo/tenderweb/TndLst.aspx （官网 → 公務採購 → 招標及判給資料 → 書面詢價）
- 筛选条件: 标书编号以 `/C/YY` 结尾（`auto` 模式自动匹配当年及上一年，免跨年维护；也可在配置中写死正则）且物品类别为「藥物」或「醫療消耗品」
- 邮件/文档/状态管理均用 Python 标准库；**抓取层需要 `curl_cffi`**（2026-09-03 起目标站启用 Cloudflare 人机验证，标准库 TLS 指纹会被 403 拦截）

## 依赖安装

```bash
pip install curl_cffi
```

## 功能特性

| 功能 | 说明 |
|------|------|
| 自动抓取 | 模拟浏览器走完 ASP.NET WebForms 会话流程（菜单→书面询价→列表），按类别筛选并自动翻页 |
| 持久化去重 | `registry.json` 注册表记录每条标书的首见时间/状态，**发送成功才标记 sent**，失败自动下次重发，绝不漏报误报 |
| Fail-closed | 抓取失败/页面结构变更/配置缺失时不发邮件、不更新状态，按退出码区分（2=抓取异常 3=发送失败 4=配置未填） |
| 每日落盘 | `data/raw_YYYY-MM-DD.json` 保存当天原始抓取，可审计回溯 |
| 周报 | `--weekly` 模式汇总本周运行统计、新增条目、未来 7 天截标提醒 |
| 漏跑补发 | `--catchup` 模式：当天 09:00 没跑（电脑没开）时自动补跑一次。**幂等 + 当日闸门**——当天一旦确认已跑过（09:00 主监控成功）或补跑成功，即写入 `last_catchup_checked` 标记，当日剩余的所有触发直接静默短路、不再抓取/发信；次日自动恢复检查。单实例锁防止并发重复发信 |
| 网络重试 | 每次请求失败就地重试 3 次, 瞬时抖动不会导致整天漏报 |
| 详情附件 | 新标书自动点进"查詢"抓取详情(項目清單/數量/備註/條款PDF直鏈/附件列表), 每条生成一份 Word(.docx) 随邮件附件发送, 存档于 data/docs/ |
| 邮件追踪行 | 每封提醒邮件末尾带 `抓取X条→符合Y条→提醒Z条` 流水线追踪 |

## 快速开始

```bash
# 1. 配置
cp config.example.ini config.ini
#    编辑 config.ini 填入 SMTP 发件邮箱、客户端专用密码、收件人

# 2. 调试运行(不发邮件, 只打印)
#    先把 config.ini 里 dry_run 改为 true
python ssm_monitor.py

# 3. 正式运行
python ssm_monitor.py            # 日常监控
python ssm_monitor.py --weekly   # 发送本周总结
python ssm_monitor.py --catchup  # 补发检查(漏跑则补跑)
```

## 定时部署

**方式一：Windows 任务计划程序**（推荐，系统级可靠）

右键“以管理员身份运行” `install_task.bat`，注册两个任务：

- `SSM_WrittenQuotation_Monitor`：每天 09:00 主监控（改时间编辑 bat 里的 `/ST 09:00`）
- `SSM_WrittenQuotation_Catchup`：**开机/登录时**补跑 `--catchup`，今天已跑过就静默退出

只想要补跑、把每日监控交给 WorkBuddy 的话，删掉 bat 里第一个 `schtasks` 行即可。两者并存也不会重复发信（有单实例锁 + 注册表去重）。

**方式二：WorkBuddy 自动化**

- 每天 09:00 运行 `python ssm_monitor.py`（主监控；务必用已装 `curl_cffi` 的 venv 解释器，标准库 TLS 会被 Cloudflare 403 拦截）
- 工作日每小时运行 `python ssm_monitor.py --catchup`（漏跑补发）：用高频检查近似"重开 WorkBuddy 即补跑"——脚本幂等，当天已成功跑过就零输出静默退出；且**当日闸门**保证：一旦确认今天无需补发，当天剩余触发全部静默短路、不再抓取，**无补发时不产生任何消息、不打扰**
- 每周五 17:00 运行 `python ssm_monitor.py --weekly`（周报）

> 早期版本为避免"每小时一条会话消息"建议一天一个检查点。当前 `--catchup` 自动化已配置为**零输出即静默结束、不回复任何消息**，因此改为工作日每小时检查是安全的：既能在 WorkBuddy 重新打开后一小时内自动补跑，又不会在平时打扰。平台无"应用启动"触发器，高频定时是唯一可行的近似方案。

## 文件说明

| 文件 | 作用 | 是否入库 |
|------|------|---------|
| `ssm_monitor.py` | 主程序（全部功能单文件） | ✅ |
| `config.example.ini` | 配置模板 | ✅ |
| `config.ini` | 实际配置（含密码） | ❌ gitignore |
| `registry.json` | 去重注册表（**勿删**，删了会全量重发） | ❌ gitignore |
| `state.json` | 上次运行日期（补发依据） | ❌ gitignore |
| `.monitor.lock` | 单实例锁（防并发重复发信，超时 15 分钟自动失效） | ❌ gitignore |
| `monitor.log` | 运行日志 | ❌ gitignore |
| `data/raw_*.json` | 每日抓取存档 | ❌ gitignore |
| `install_task.bat` | Windows 定时任务安装脚本 | ✅ |
| `DEV_DOC.md` | v2 开发文档（设计/边界/验收） | ✅ |

## 技术要点

- 目标是 ASP.NET WebForms 站点，所有筛选/翻页都是表单回传（postback）。**关键坑**：提交时必须带上表单全部字段（含 `__VIEWSTATEENCRYPTED`），缺任何一个服务器都返回“網頁發生錯誤”。
- 物品类别下拉值：`M`=藥物、`2`=醫療消耗品、`R`=試劑 等，见页面源码。
- 密码支持环境变量 `SMTP_PASSWORD` 覆盖配置文件，避免明文落盘。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 正常（含无新增） |
| 2 | 抓取异常/页面结构疑似变更 |
| 3 | 邮件发送失败（条目保持 pending，下次重发） |
| 4 | SMTP 配置未填写 |
