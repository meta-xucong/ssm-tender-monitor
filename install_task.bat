@echo off
chcp 65001 >nul
REM ==========================================================
REM 安装 SSM 书面询价监控的 Windows 计划任务(需"以管理员身份运行")
REM
REM 任务1  SSM_WrittenQuotation_Monitor : 每天 09:00 主监控
REM 任务2  SSM_WrittenQuotation_Catchup : 每次开机/登录时补跑
REM        (今天 09:00 已成功跑过就静默退出, 不会重复发信)
REM
REM 说明: 如果已经用 WorkBuddy 自动化跑每日 09:00 监控, 可以只装任务2,
REM        把任务1 那行删掉即可, 两者同时存在也不会重复发信(有单实例锁)。
REM ==========================================================
set PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
set SCRIPT=C:\Users\Administrator\WorkBuddy\2026-09-02-10-16-53\ssm_monitor.py

schtasks /Create /TN "SSM_WrittenQuotation_Monitor" /TR "\"%PYEXE%\" \"%SCRIPT%\"" /SC DAILY /ST 09:00 /F
echo.
schtasks /Create /TN "SSM_WrittenQuotation_Catchup" /TR "\"%PYEXE%\" \"%SCRIPT%\" --catchup" /SC ONLOGON /F
echo.
echo 若上面显示"成功", 可用以下命令查看:
echo   schtasks /Query /TN "SSM_WrittenQuotation_Monitor"
echo   schtasks /Query /TN "SSM_WrittenQuotation_Catchup"
echo 删除任务: schtasks /Delete /TN "任务名" /F
pause
