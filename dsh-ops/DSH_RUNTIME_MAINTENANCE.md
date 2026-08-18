# DSH 运行维护手册（2026-08-18 首版）

> 目的：DSH（DeepSeek Harness）是 Trinity 的插件宿主。本文记录其正确启动方式、
> 进程拓扑、常见问题与恢复步骤——避免再次出现"双宿主 + 错误日志刷屏"。

## 一、正确启动方式（重要）

| 方式 | 命令 | 结果 |
|---|---|---|
| ✅ **正确** | `dsh web`（全局 CLI，npm 全局 @deepseek-ai/dsh） | 启动 web 宿主，监听 **127.0.0.1:3080** |
| ❌ 错误 | `npx @deepseek-ai/dsh web` | **启动必失败**：npx 把 `web` 当模块 require（`Cannot find module 'C:\Users\Administrator\web'`），产生僵尸进程 + 刷屏 err.log |

- dsh CLI 路径：`C:\Users\Administrator\AppData\Roaming\npm\dsh.cmd`（入口 node `...\@deepseek-ai\dsh\lib\bin.js`）
- 版本配套：**dsh-trinity 插件版本必须 == @deepseek-ai/dsh 版本**（当前均 0.1.0-rc.6）；
  插件位于 `C:\Users\Administrator\trinity\dsh-plugin\dsh-trinity\`，改 JS 后需重启 web 宿主生效（HMR 仅 client-plugin 有效，host 侧插件需重启）。

## 二、进程拓扑（正常态）

```
dsh web 宿主 (node, PID 变化)  ── 监听 127.0.0.1:3080
  ├─ 加载 dsh-trinity 插件 (17 个 trinity_* 工具)
  ├─ engine_worker.py (python, 插件 spawn, 卡死自愈)
  │    └─ SQLite 大库 ~/.trinity/store/trinity_store.db
  └─ 结构层同步 → dsh_events/dsh_sessions/dsh_goals...
```

判定工作宿主：`Get-NetTCPConnection -LocalPort 3080 -State Listen` 的 OwningProcess 即当前 GUI 宿主。
其余 node 进程若无 3080 监听且无 dsh 相关子进程 → 大概率是残留，可清理。

## 三、日志

| 文件 | 说明 |
|---|---|
| `~/.dsh/logs/web.out.log` | 启动输出（含端口） |
| `~/.dsh/logs/web.err.log` | 错误日志（曾因 npx 误用刷 653KB；正常时很小） |

## 四、常见问题与恢复

1. **双 web 宿主**：一个 3080 工作宿主 + 一个 npx 僵尸（无端口、内存小）。
   → 杀掉僵尸：`Stop-Process -Id <pid> -Force`（先确认它不监听 3080）；清 `web.err.log`。
2. **err.log 刷 MODULE_NOT_FOUND**：某次用了 `npx ... web`。→ 用 `dsh web` 重启；清日志。
3. **插件工具不可用**（trinity_* 消失）：插件 JS 改动后未重启 / worker 卡死。
   → 重启 web 宿主（注意：会中断当前会话，先备份会话上下文）；worker 有自愈（插件自动重启）。
4. **engine_worker 卡死**：ping 超时 → 插件自动杀/重启（看门狗 90s）；仍不行手动杀 worker 进程。
5. **web 宿主未自启**：登录后无 3080 → 手动 `dsh web`，或启用 `start-dsh-web` 自启脚本（见下）。

## 五、自启（可选，2026-08-18 提供）

- 脚本：`dsh-ops/start-dsh-web.ps1`（守卫：3080 已监听则跳过；否则拉起 `dsh web` 并记日志 `~/.dsh/logs/dsh-web-autostart.log`）
- 安装：把 `dsh-ops/dsh-web-autostart.vbs` 放入 Startup 文件夹（登录时以隐藏窗口运行）；
  与 trinity-dsh-autostart.vbs（Trinity 维护链）互不干扰。
- 卸载：从 Startup 删除 VBS 即可。
- ⚠️ 注意：自启只应在"登录后没有 web 宿主"时拉起；若已有手动宿主（3080 占用），守卫自动跳过。

## 六、环境要求（DSH 侧）

- Node.js（当前 v25.2.1）+ npm 全局 `@deepseek-ai/dsh`；
- dsh-trinity 插件（trinity 仓库 dsh-plugin/）+ 系统 Python 3.14（engine_worker 用）；
- 凭证 `~/.dsh/.credentials.yaml`（DEEPSEEK_API_KEY 等，与 Trinity 共用）；
- 端口 3080（web GUI，仅 127.0.0.1）。
