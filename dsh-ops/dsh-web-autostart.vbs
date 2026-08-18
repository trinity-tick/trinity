Set WshShell = CreateObject("WScript.Shell")
' DSH web 宿主登录自启（2026-08-18）— 守卫：3080 已监听则跳过
WshShell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\Administrator\trinity\dsh-ops\start-dsh-web.ps1""", 0, False
Set WshShell = Nothing
