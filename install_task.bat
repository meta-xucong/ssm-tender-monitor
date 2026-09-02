@echo off
chcp 65001 >nul
REM 创建"SSM书面询价监控"每日定时任务(每天 09:00 运行)
REM 如需修改时间, 把 /ST 09:00 改成想要的时间即可
schtasks /Create /TN "SSM_WrittenQuotation_Monitor" /TR "\"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe\" \"C:\Users\Administrator\WorkBuddy\2026-09-02-10-16-53\ssm_monitor.py\"" /SC DAILY /ST 09:00 /F
echo.
if %errorlevel%==0 (echo 任务创建成功! 可用 schtasks /Query /TN "SSM_WrittenQuotation_Monitor" 查看) else (echo 创建失败, 请尝试右键"以管理员身份运行")
pause
