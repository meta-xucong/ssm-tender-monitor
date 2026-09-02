# 澳门网页招标信息监控 (SSM Tender Monitor)

自动监控**澳门卫生局(SSM)书面询价**页面，发现符合条件的**新标书**时邮件提醒，每周五发送监控周报。

- 监控页面: https://www.ssm.gov.mo/tenderweb/TndLst.aspx （官网 → 公務採購 → 招標及判給資料 → 書面詢價）
- 筛选条件: 标书编号含 `/C/26`（即 2026 年度书面询价，可在配置中按年调整）且物品类别为「藥物」或「醫療消耗品」
- 纯 Python 标准库实现，零第三方依赖

## 功能特性

| 功能 | 说明 |
|------|------|
| 自动抓取 | 模拟浏览器走完 ASP.NET WebForms 会话流程（菜单→书面询价→列表），按类别筛选并自动翻页 |
| 持久化去重 | `registry.json` 注册表记录每条标书的首见时间/状态，**发送成功才标记 sent**，失败自动下次重发，绝不漏报误报 |
| Fail-closed | 抓取失败/页面结构变更/配置缺失时不发邮件、不更新状态，按退出码区分（2=抓取异常 3=发送失败 4=配置未填） |
| 每日落盘 | `data/raw_YYYY-MM-DD.json` 保存当天原始抓取，可审计回溯 |
| 周报 | `--weekly` 模式汇总本周运行统计、新增条目、未来 7 天截标提醒 |
| 漏跑补发 | `--catchup` 模式：若当天定时任务没跑（电脑没开），下次开机后自动补跑 |
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

右键“以管理员身份运行” `install_task.bat`，注册每天 09:00 的任务。改时间编辑 bat 里的 `/ST 09:00`。

**方式二：WorkBuddy 自动化**

- 每天 09:00 运行 `python ssm_monitor.py`
- 每小时运行 `python ssm_monitor.py --catchup`（漏跑补发）
- 每周五 17:00 运行 `python ssm_monitor.py --weekly`（周报）

## 文件说明

| 文件 | 作用 | 是否入库 |
|------|------|---------|
| `ssm_monitor.py` | 主程序（全部功能单文件） | ✅ |
| `config.example.ini` | 配置模板 | ✅ |
| `config.ini` | 实际配置（含密码） | ❌ gitignore |
| `registry.json` | 去重注册表（**勿删**，删了会全量重发） | ❌ gitignore |
| `state.json` | 上次运行日期（补发依据） | ❌ gitignore |
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
