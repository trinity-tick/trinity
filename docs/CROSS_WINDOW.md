# Trinity v6.37 — 跨窗口共享方案
# ================================
# 问题： 每次开新窗口都要重新设置？
# 解决： 所有窗口共享 ~/.trinity/ 目录，任何窗口运行 trinity_init.py 即可

# ─────────────────────────────────────────────────────────────────
# 方式 A: 在任何窗口中运行这一行（推荐）
# ─────────────────────────────────────────────────────────────────
cd C:\Users\Administrator\trinity && python trinity_init.py init

# ─────────────────────────────────────────────────────────────────
# 方式 B: 双击 start_trinity.bat（Windows 桌面）
# ─────────────────────────────────────────────────────────────────
# 已创建文件: C:\Users\Administrator\trinity\start_trinity.bat

# ─────────────────────────────────────────────────────────────────
# 方式 C: 直接在 Goose/Claude 中粘贴（在任何聊天窗口中都适用）
# ─────────────────────────────────────────────────────────────────
# 把下面这段话粘贴到任何聊天窗口，Trinity 就会恢复所有记忆：
"""
请帮我执行以下操作：
1. 运行 C:\Users\Administrator\trinity\trinity_init.py init（初始化进化引擎）
2. 读取 ~/.trinity/evolution_state.json（恢复之前的进化状态）
3. 读取 ~/.trinity/handoff_*.json（如果有 handoff 文件）
4. 告诉我当前进化周期数和已记忆的模式
"""

# ─────────────────────────────────────────────────────────────────
# 方式 D: Python 一行启动（适用于任何支持 Python 的 Agent）
# ─────────────────────────────────────────────────────────────────
python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\trinity'); from trinity.evolution import MetaEvolution; evo = MetaEvolution(); evo.tick({'init': True}); evo.save_state(); d = evo.diagnostics(); print(f'Trinity恢复: {d[\"total_cycles\"]}周期, {d[\"preferences_count\"]}偏好, {d[\"patterns_count\"]}模式')"

# ─────────────────────────────────────────────────────────────────
# 验证状态
# ─────────────────────────────────────────────────────────────────
python trinity_init.py check

# ─────────────────────────────────────────────────────────────────
# "一次设置，所有窗口共用" 的核心原理
# ─────────────────────────────────────────────────────────────────
# 所有 Trinity 状态存储在 ~/.trinity/（用户的 HOME 目录）
# ─── evolution_state.json — 进化引擎状态（周期数、偏好、模式、修正记录）
# ─── handoff_*.json       — 窗口交接文件（跨窗口传递状态）
# ─── trinity_cross_state.json — 冗余备份
#
# 不管你在哪个窗口/哪个 Agent 中运行 trinity_init.py，
# 读取的都是同一个 ~/.trinity/ 目录。
# 这就是跨窗口共享的本质——文件系统是全局的，状态是全局的。
