# trinity-sync-agent 接入说明（Windows 守护部署）

> 目标：把多机同步代理作为可随时启用的守护能力。**默认不启用**，配置好远端服务器后才开启，
> 避免误把本地大库推送到本机聚合池造成污染。

## 1. 前置（必须先完成）
1. 有一台**服务器** Trinity（本地或远端，含 /agents/memory/bulk_write 端点）。
2. 本机装好系统 Python 3.10+ 且已装 `requests`：
   ```powershell
   & "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe" -c "import requests"
   ```
3. 把 `dsh-ops\sync-agent.yaml.template` 复制为 `C:\Users\Administrator\.trinity\sync-agent.yaml`：
   - `server.url` → 填**远端服务器**地址（如 `https://memory.example.com` 或内网 `http://10.0.0.5:8001`），
     **不要填本机 127.0.0.1**（那会把本机大库推回本机聚合池，污染检索面）。
   - `server.api_key` → 服务器若开了 TRINITY_API_KEY 则填本机专用 Key。
   - `server.machine` → 本机唯一标识（如 `pc-1`）。
   - `cursor.file` / `sync.*` 按需。

## 2. 手动单步 / 自检
```powershell
# 只跑一轮（验证本机 → 服务器推送 + 游标）
python trinity-sync-agent.py --one
# P0 概念验证（临时库模拟，不碰真实库）
python trinity-sync-agent.py --p0
```

## 3. 连续守护（两种方式，二选一）

### 方式 A：计划任务（推荐，免提权）
`install-sync-agent-schedule.bat` 已注册到 `dsh-ops\`；或手动：
```powershell
schtasks /create /tn "trinity-sync-agent" /tr "C:\...\Python314\python.exe C:\Users\Administrator\trinity\dsh-ops\trinity-sync-agent.py --loop" /sc onlogon
```
> 用 `--loop` + onlogon 计划任务满足"常驻连续推送"；3s 轮询靠脚本内 sleep，无需系统级每 X 分钟调度。

### 方式 B：启动文件夹 VBS（登录自启，免提权）
把下面的 `trinity-sync-agent-autostart.vbs` 放入
`shell:startup`（或与本仓库 `dsh-ops\` 的 trinity-autostart 链共用），
改为你的 Python 路径后使用：

```vbs
' trinity-sync-agent-autostart.vbs — 登录自启（隐藏窗口）
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"" ""C:\Users\Administrator\trinity\dsh-ops\trinity-sync-agent.py"" --loop", 0, False
```

## 4. 日志与回滚
- 日志：`C:\Users\Administrator\.trinity\logs\sync-agent.log`（每次推送 sent/fail）。
- 回滚：
  - 删计划任务：`schtasks /delete /tn "trinity-sync-agent" /f`
  - 删 VBS：启动文件夹里移除该 .vbs
  - 删 `C:\Users\Administrator\.trinity\sync-agent-cursor.json`（重置游标→重新全量）
- 无对服务器/源码的侵入性改动。

## 5. 关键安全边界
- **默认指远端**：不要给 `sync-agent.yaml` 填本机 127.0.0.1，除非那是真正的另一台服务器。
- 同步范围可用 `sync.filter.min_importance` / `categories` 收紧（二期实现）。
- 每台机器独立 api_key，可单独吊销。
