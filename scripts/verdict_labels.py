#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verdict_labels.py — 结论标签必须**自证含义**（2026-09-21 §1050）

约定（把 AGENTS.md §13.2 的举例提升为通用规则）
------------------------------------------------
任何工具给出的结论标签（PASS / REJECT / INCONCLUSIVE / PASS_RISK_ONLY…）都必须在这里
**登记一条含义**，并声明它**是不是"批准性结论"**。理由（实测动机）：

  · `PASS_RISK_ONLY` 只表示"风险侧没有量到下行"，而自检索充分性在该语料上不成立
    ⇒ 它**不是**启用批准。若标签不自证含义，下一个人会把它当 APPROVED 用。
  · 相反的坑同样存在：`INCONCLUSIVE` 常被读成"失败"，而它真正的含义是"读数不可信/样本不足"
    ⇒ 需要采取的动作完全不同（补数据 vs 改实现）。

用法：`from verdict_labels import meaning, is_approval`；
判据：`tests/unit/test_verdict_labels.py`（含"使用方必须 import 本模块"的源码级检查）。
"""
from __future__ import annotations

#: 标签 → (含义, 是否批准性结论)
VERDICT_LABELS = {
    "PASS": ("判据全部满足，可作为批准性结论使用", True),
    "REJECT": ("判据明确不满足（量到了下行），应停止该变更", False),
    "INCONCLUSIVE": ("读数不可信或样本不足 ⇒ **这不是结论**；动作是补数据/修取数，不是改实现", False),
    "PASS_RISK_ONLY": ("只说明风险侧没有量到下行；**不是启用批准**（充分性未验证）", False),
    "ATTENTION": ("读数越过警戒线但未判失败 ⇒ 动作是盯，不是改", False),
    # 2026-09-21（§1051）：把**其余工具已在用的决策标签**补登记（实测扫描出来的真实词汇表）。
    # 只登记我能如实写出含义的；扫描发现的其余标签（GREEN/AT_RISK/UNTESTABLE 之外的状态词）
    # 由 scan_unregistered() 列成**待办**，不假装已覆盖。
    "FAIL": ("判据未满足（与 REJECT 同义，按工具习惯使用）", False),
    "NO_EFFECT": ("变更前后没有可测差异 ⇒ 不是成功也不是失败，是**没效果**", False),
    "UNTESTABLE": ("当前条件下无法测量 ⇒ 动作是补数据/换探针，不是改实现", False),
    "OK": ("读数正常 ⇒ 无需动作（与 PASS 不同：它不构成任何批准）", False),
    # 2026-09-21（§1052）：按**实现在场**的语义补登记（每条含义都能对着代码行指认，不编占位）。
    "GREEN": ("该层判据全部通过（血流分层的层内读数）；**不构成跨层批准**", False),
    "YELLOW": ("层内读数越过警戒线但未判失败 ⇒ 动作是盯", False),
    # 2026-10-06：登记本会话新增工具实际发出的结论标签（`test_verdict_labels` 棘轮扫出来的）。
    # 每条含义都能对着发它的代码行指认，不编占位。
    "SIGNIFICANT": ("配对/统计检验达到显著（p<0.05）⇒ 支持该差异真实存在；"
                    "**不构成改默认的批准**（还要看效应量与下游作用域）", False),
    "NOT_SIGNIFICANT": ("统计检验未达显著 ⇒ **不得据此改默认**；"
                        "与 INCONCLUSIVE 区分：这里**有**读数，只是差异落在噪声内", False),
    "DRY_RUN": ("只做计划与报数、**未写入任何行** ⇒ 这不是执行结果；"
                "动作是核对数字后再决定是否 apply", False),
    "FAILED": ("执行/构建**失败** ⇒ 动作是查错重试；"
               "与 FAIL 区分：FAIL 是判据不满足，FAILED 是流程本身没跑成", False),
    "WINDOW_OK": ("资源窗口检查通过（commit-free 余量充足）⇒ 允许跑重活；"
                  "**不是**任何变更的批准", False),
    "RED": ("层内读数判失败 ⇒ 需要处置（是否阻塞取决于该层的语义）", False),
    "FRESH": ("探针数据在新鲜度阈值内 ⇒ 读数可用", False),
    "FRESH_OK": ("服务新鲜度探针通过（与 NOT_RANKED 并列的旁路状态）", False),
    "COVERAGE_PARTIAL": ("只覆盖了部分语料 ⇒ 判据**只对覆盖到的部分**成立", False),
    "NO_PROD_READ": ("生产侧没有被读到 ⇒ **无法判定**（不是失败）", False),
    "OPT_IN_SET": ("处于 opt-in 集合（默认不启用）⇒ 不参与判定", False),
    # 2026-09-21（§1118）：本会话新增工具 fok_backlog_trend.py 的三支判据补登记
    # （棘轮测试当场抓到 ACK_OK 未登记 —— 这正是它该做的事）。
    "ACK_OK": ("趋势判据成立：该环的 ACK 仍有效（已复核·已知且正在收敛）⇒ **不构成批准**", False),
    "REGISTERED": ("趋势走平/反弹，但同期**有新增登记** ⇒ 先查是谁在登记，别当成退化", False),
    "MECHANISM_CHANGED": ("趋势走平/反弹且**无新增** ⇒ 机制变了 ⇒ 应撤 ACK、重新定性", False),
    # 2026-09-21（§1168）：重启工具的失败归因补登记（棘轮测试第二次抓到未登记标签）。
    # 含义要写清"它不是什么"：integrity_boundary 只说明**重启没被执行**，
    # **不等于**服务故障、也不等于改动有问题 ⇒ 动作是提权重试，不是去查服务。
    "integrity_boundary": ("调用方完整性低于目标进程（Medium 杀不动 High）⇒ **重启没被执行**；"
                           "既不是服务故障，也不是改动有问题 ⇒ 动作是**提权**重试", False),
    # 2026-09-22（§1247）：`supervisor_leg_status.py::classify()` 的**监督腿**五档结论补登记。
    # 棘轮测试当场只抓到 `LEG_CHURN` / `LEG_SETTLED`（因为它俩出现在比较与 return 位），
    # 但这个工具**实际会发五个** ⇒ 这里按**完整词汇表**登记：
    # 只补被扫到的那两个，等于「让判据变绿」而不是「让词汇表完整」。
    # 含义逐条对着实现行确认过（classify()：n_legs / restarts_60m / sup_age_min / last_start_age_min）。
    "LEG_OK": ("监督腿健康：实例数 == 1 且 60 分钟内重启 < 3 且监督产物新鲜", False),
    "LEG_DOWN": ("监督腿**没有实例在跑** ⇒ 需要拉起（不是「慢」，是没人）", False),
    "LEG_DUPLICATE": ("监督腿有 **≥2 个实例** ⇒ 两条腿互相重置计时器，必须处置", False),
    "LEG_CHURN": ("腿在**打转**：60 分钟内重启 ≥ 3 **且**最近一次重启仍在 `CHURN_RECENT_MIN` 内；"
                  "或没在打转但产物陈旧（> `STALE_SUP_MIN`）⇒ 同样是节奏不对", False),
    "LEG_SETTLED": ("计数上打转过（≥3 次/60 分钟），但最近一次重启已超出 `CHURN_RECENT_MIN` "
                    "⇒ **曾经**打转、现在稳；计数照报，不许静默丢（§13.0「剔除要报数」）", False),
    # 2026-09-28 补登记：同一个 `supervisor_leg_status.py::classify()` 的**第六档**。
    # §1247 当时按「完整词汇表」登记了五档，但该工具自己的 want 列表
    # （supervisor_leg_status.py:412）里就有 ENUM_RESTRICTED ⇒ 这是**漏登记**，不是新标签，
    # 由 tests/unit/test_verdict_labels.py 的棘轮当场抓到（它正是为此存在）。
    # 语义对着实现与 checklist_run.py:300 的 E7 说明逐字确认：
    # 本上下文的进程枚举被裁剪（受限令牌/低完整性）⇒ n_legs==0 是「取不到」而非「不存在」。
    # 它必须是 fail-closed：判不了就当判不了，**绝不当 OK**（rc=3）。
    "ENUM_RESTRICTED": ("本上下文的**进程枚举被裁剪**（受限令牌/低完整性）⇒ 0 实例是「取不到」"
                        "而不是「不存在」，**既不等于腿死了也不等于腿活着** ⇒ 动作是换一个"
                        "非受限 shell 复跑本探针，不是去拉起腿（rc=3，fail-closed，不当 OK）", False),
    "EFFECTIVE": ("变更被量到有效", False),
    "PARSE_ERROR": ("解析失败 ⇒ 读数不可信，动作是修取数", False),
    "AT_RISK": ("有风险迹象但未失败 ⇒ 动作是盯", False),
    "NOT_RANKED": ("该探针未参与排名 ⇒ **不构成比较结论**", False),
    "STALE_VIEW": ("读到的是过期视图 ⇒ 先刷新再判", False),
    "NEVER_ACTIVE": ("从未被激活过 ⇒ 没有可判定的样本", False),
    "PENDING": ("尚未产出/待完成 ⇒ 不可判", False),
    "UNREACHABLE": ("端点不可达 ⇒ 读数不可信（与「没数据」是两回事）", False),
    # 2026-09-21（§1055）：小写状态词批次 1（只登记语义明确的**决策**词；其余留在棘轮基线里当待办）
    "adopt": ("采纳该路由/机制声明（与 reject 对称）；**不是全局批准**", False),
    "keep_default": ("维持默认行为（即不采纳该声明）", False),
    "reject": ("不采纳该声明（判据未通过）", False),
    "quality_gain": ("相对基线有质量增益（该维度的局部结论）", False),
    "retire": ("建议退役（有生产无消费/已被替代）", False),
    # 2026-09-21（§1056）小写批次 2：**接线审计**这一组（语义成组，一起登记；含义对着代码行确认过）
    "allowlisted": ("该模块在豁免名单里 ⇒ **不要求接线**（不是缺陷）", False),
    "unwired": ("没有任何消费者 ⇒ **未接线**（生产者在、回路不在）", False),
    "wireable": ("可接线（已有消费者候选）", False),
    "wireable_unscheduled": ("可接线但**未排期** ⇒ 是待办，不是失败", False),
    "candidate_unconsumed": ("已是候选但**无人消费**", False),
    "only_self_read": ("只有生产者自己读（自证）⇒ **不算接线**", False),
    "no_path": ("提案里根本没写路径", False),
    "path_missing": ("提案里的路径**不存在**", False),
    "path_ok": ("路径检查通过（仅说明路径存在，不构成批准）", False),
    # 2026-09-21（§1057）小写批次 3：**对比实验**这一组（语义成组：都是"相对基线"的结论）
    "no_effect": ("相对基线**没有可测差异** ⇒ 不是成功也不是失败，是没效果", False),
    "slower": ("相对基线**更慢**（延时的下行）", False),
    "yield_loss": ("相对基线**产出变少**（召回/覆盖的下行）", False),
    "candidate": ("**候选**状态：够格但尚未采纳（不构成批准，也不构成失败）", False),
    "unknown": ("无法归类 ⇒ 动作是**补判据/补数据**，不是改实现", False),
    # 2026-09-21（§1057）第三类盲区的三个词（只出现在**比较**里，前两版正则看不见）
    "idle_by_design": ("该状态文件按设计闲置 ⇒ **不是漏接**", False),
    "existence_only": ("只有存在性、没有消费 ⇒ **空壳**", False),
    "dormant_observed": ("观察到休眠：有生产无消费但**符合预期**（与 NEVER_ACTIVE 不同）", False),
    # 2026-09-21（§1059）批次 4：按**实现**登记（每条含义都能指认到代码行）
    "fresh": ("状态文件 mtime < 6h ⇒ 新鲜（brain_heartbeat）", False),
    "missing": ("状态文件不存在（brain_heartbeat）", False),
    "idle": ("6h~7d 未更新：事件驱动场景下**属正常**（≠失败）", False),
    "STALE": ("状态文件 >7d 未更新 ⇒ **需排查**（与 idle 的区别在时长与是否事件驱动）", False),
    "insufficient": ("有效采样点不足（如 <3）⇒ **不能判定**，动作是补数据", False),
    "support_full_context": ("增益 ≥ 0.05 ⇒ 证据**支持**全上下文（sufficiency_gate）", False),
    "no_gain_consistent_with_583": ("增益不足：与 §583 的既往结论一致 ⇒ **不支持**全上下文", False),
    "no_bias_detected": ("未检出归档偏差（forgetting_bias_audit）", False),
    "archive_biased_to_negative": ("归档**偏向**负面记忆 ⇒ 遗忘偏差", False),
    "archive_biased_away_from_negative": ("归档**回避**负面记忆 ⇒ 反向偏差（同样要处置）", False),
    "FIXED": ("平均返回 ≥ 8 ⇒ 修复**已生效**（activation_scope_probe）", False),
    "OLD": ("平均返回 ≤ 3 ⇒ **仍是旧行为**（≠ FIXED）", False),
    # 2026-09-21（§1059）批次 5：这两个不是「工具结论」，而是**预测校验器的判定结果**
    # （brain_cycle.py 对上轮预测自动判定 yes/no/partial，用于 regret；yes=1.0、partial=0.5）。
    # 登记它们是因为它们确实出现在 verdict 字段里 —— 名字相同、语义不同，正是登记表要防的误读。
    "yes": ("上轮预测被验证为**成立**（预测校验器；计 1.0）", False),
    "partial": ("上轮预测**部分成立**（计 0.5）", False),
    # 2026-09-23（§1306）：全量回归的 `test_verdict_labels` 棘轮报出 7 个未登记标签 ——
    # 都是**当天新增的工具**发出来的（`TIMEOUT` 来自 §1302 的 `fulltest_gate.py`），
    # 按本表纪律逐个登记含义（含义取自各工具 docstring 的原文，不是另编的）。
    "TIMEOUT": ("跑超时被杀 ⇒ **没有结论**（≠「用例全挂了」）；动作是查为什么慢、或显式放宽上限"
                "（fulltest_gate.py §1302）", False),
    "DEAD_SWITCH": ("开关是**死的**：在多个互不相同的查询上取值恒定（含全 null）⇒ "
                    "`p_source` 字符串写了什么**不算证据**（prob_source_probe.py §1277）", False),
    "UNVERIFIED": ("接口取不到 / 响应形状不符 ⇒ **绝不把「没取到」读成「没有源」**"
                   "（§13.2；prob_source_probe.py）", False),
    "IDENTITY": ("无映射文件 ⇒ 恒等映射，**不判**（fok_map_degeneracy_probe.py）", False),
    "MAP_FLAT": ("映射输出近似常数（跨度 < 0.05）⇒ 档位无信息（同上）", False),
    "MAP_BINARY_STEP": ("输出只有 {0.0, 1.0} 两档 ⇒ 阶跃，档内全平（同上）", False),
    "MAP_GRADED": ("输出 ≥3 档且跨度 ≥0.05 ⇒ 保留了分级信息（同上）", False),
    # 2026-10-07（t87/B3 A5-03）：两库一致性/滞后判据
    # `scripts/cross_store_reconcile_probe.py` 的三个结论标签补登记
    # （棘轮测试 `test_verdict_labels` 当场抓到未登记 —— 这正是它该做的事）。
    # 含义逐条对着发它的代码行指认（reconcile() 末尾的 verdict/rc 分支），rc 映射逐字照搬 t87 设计。
    "CONSISTENT": ("**两库一致**：跨库存在性 ≥ `min_exists_ratio` 且滞后 ≤ `max_lag_seconds`（rc=**0**）；"
                   "口径：跨库=同 memory_id 在对侧可查的比例；注意它与同库对照组"
                   "（`self_exists_ratio`，应≈1.0）**不得并列**成'一致性'", False),
    "DIVERGENT": ("**两库真不一致**（rc=**1**）：`reason` 细分为 `missing_rows`（跨库存在性低于阈值）/ "
                  "`lag_exceeded`（最新可见行的滞后超阈值）/ `both`；"
                  "⚠️ 与 `INCOMPARABLE` **必须区分** —— 只有「读数正常但跨库缺行/滞后」才配得上这个标签", False),
    "INCOMPARABLE": ("**口径不适用**（rc=**3**，沿 t77 的 rc=3 思路）：两侧是同一个库 / "
                     "一侧无样本 / 或**同库自检不达标**（`reason=self_check_failed` ⇒ 读数或配置坏了）。"
                     "⇒ **不得**把它读成'不一致'、**更不得**当成'服务有问题'；动作是修读数/配置，不是去查数据", False),
}


def annotate(out: dict, key: str = "verdict") -> dict:
    """把结论标签的含义**写进输出本身**（2026-09-21 §1053）。

    动机（实测 §1050-§1052）：标签登记了，但工具仍只打印 `verdict=PASS` 这种裸词 ——
    `NO_EFFECT` 会被读成"机制无效"、`INCONCLUSIVE` 会被读成"失败"（`gw_boost_ab.py` 的注释里
    就写着这个担忧）。把含义**随结论输出**，读的人不必回头查登记表。
    判据：`tests/unit/test_verdict_labels.py` 断言 annotate 真的注入两个字段，且**使用方必须调用它**。
    """
    try:
        lbl = str((out or {}).get(key) or "")
        if lbl:
            out["verdict_meaning"] = meaning(lbl)
            out["verdict_is_approval"] = is_approval(lbl)
    except Exception:  # noqa: BLE001 — 未登记标签不阻塞输出，但会留痕
        out["verdict_meaning"] = "(未登记的结论标签：%s)" % str((out or {}).get(key))
        out["verdict_is_approval"] = False
    return out


def label_with_meaning(out: dict, key: str = "verdict") -> str:
    """终端行统一格式：`LABEL（含义）`（2026-09-21 §1054）—— 免去各工具各写各的格式。"""
    lbl = str((out or {}).get(key) or "")
    m = (out or {}).get("verdict_meaning")
    if not lbl:
        return "(无结论)"
    return "%s（%s）" % (lbl, m) if m else lbl


def scan_unregistered(root: str) -> dict:
    """扫描 scripts/*.py 里实际发出的 verdict 标签，返回**未登记**的那些（待办清单）。

    2026-09-21（§1051）：把 §1050 的约定从"一个工具"推广到"全部工具"的第一步 ——
    先用扫描把**真实词汇表**拿出来（实测 19 个），再逐个登记含义；
    未登记的**列出来**而不是静默放过。
    """
    import os as _os
    import re as _re
    # 2026-09-21（§1055）：正则放宽到**大小写都认** —— 上一版只认大写（[A-Z][A-Z_]{2,}），
    # 于是 blood_flow_status 里的小写状态词（existence_only / idle_by_design / dormant_observed）
    # 全都**不在扫描范围内**，而"词汇表已清空"那句话当时没说清这个范围。**盲区比漏登记更危险**。
    # 2026-09-21（§1055 两段式）：① 只认大写时漏掉小写状态词（盲区）；② 放宽后**过量捕获**
    # （把 `"verdict_llm": "yes"` 这种相邻键也当结论标签，扫出 32 个里混着 assert/llm/yes）。
    # ⇒ 键必须**独立**出现：`"verdict"` 或 `verdict`（后面不能再跟单词字符），值取整个字符串字面量。
    # 2026-09-21（§1057）**第三类盲区**：只出现在**比较**里的标签
    # （例如 r.get("verdict") == "idle_by_design"、in ("existence_only", "dormant_observed")）
    # 既不赋值、也不做字典值 ⇒ 前两版正则全都看不见。现同时扫「赋值」与「比较」两种出现方式。
    pat = _re.compile(r"(?:[\"']verdict[\"']|\bverdict)(?![A-Za-z_])\s*[:=]\s*[\"']([A-Za-z][A-Za-z_]{2,})[\"']"
                      r"|(?:[\"']verdict[\"']|\bverdict)(?![A-Za-z_])\)?\s*(?:==|!=|in)\s*[\(\[]?[\"']([A-Za-z][A-Za-z_]{2,})[\"']")
    out: dict = {}
    try:
        for fn in sorted(_os.listdir(root)):
            if not fn.endswith(".py"):
                continue
            try:
                src = open(_os.path.join(root, fn), encoding="utf-8", errors="ignore").read()
            except Exception:  # noqa: BLE001
                continue
            for m in pat.finditer(src):
                lbl = m.group(1) or m.group(2)
                if lbl not in VERDICT_LABELS:
                    out.setdefault(lbl, set()).add(fn)
    except Exception:  # noqa: BLE001
        return {}
    return {k: sorted(v) for k, v in sorted(out.items())}


def meaning(label: str) -> str:
    """取标签含义；未登记的标签直接抛错（不静默放行）。"""
    try:
        return VERDICT_LABELS[label][0]
    except KeyError:
        raise KeyError("未登记的结论标签：%r（先在 verdict_labels.VERDICT_LABELS 里写清含义）"
                       % (label,))


def is_approval(label: str) -> bool:
    """该标签是否算"批准"（只有 PASS 算）。"""
    return bool(VERDICT_LABELS.get(label, (None, False))[1])
