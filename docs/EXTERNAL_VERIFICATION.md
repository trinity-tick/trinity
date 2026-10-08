# 外部复核包（EXTERNAL VERIFICATION）

> **本文件由 `scripts/scores_gate.py --emit-doc` 生成，请勿手改。**
> 重新生成：`python scripts/scores_gate.py --emit-doc docs/EXTERNAL_VERIFICATION.md`

目的：让**外部方**不必相信 Trinity 的自述，能用下面每一行的「命令 + 产物 + SHA-256」自行复核数字。

## 1. 逐条登记（claimable 优先）

| id | 指标 | 值 | 协议 | n | 日期 | 可对外 | 产物 SHA-256（前 16 位） |
|---|---|---|---|---|---|---|---|
| `lme_s_full120_adapter_20260913` | AnswerAcc | 0.5 | full-haystack | 120 | 2026-09-13 | ✅ | `64c66b279b125f58` |
| `lme_s_full120_hybrid_20260914` | AnswerAcc | 0.55 | full-haystack | 120 | 2026-09-14 | ✅ | `8e2838cb9a815caa` |
| `lme_s_full120_qa_20260911` | AnswerAcc | 0.4917 | full-haystack | 120 | 2026-09-11 | ✅ | `9d450f38a2c87a47` |
| `lme_s_full120_retrieval_20260911` | R@1/R@5/R@10 | [0.7917, 0.85, 0.9167] | full-haystack | 120 | 2026-09-11 | ✅ | `9d450f38a2c87a47` |
| `lme_s_full500_qa_20260911` | AnswerAcc | 0.612 | full-haystack | 500 | 2026-09-11 | ✅ | `a85dc5422d2dd262` |
| `lme_s_full500_refresh_qa_20260917` | AnswerAcc | 0.61 | full-haystack | 500 | 2026-09-17 | ✅ | `db87551471f90029` |
| `lme_s_full500_refresh_retrieval_20260917` | R@1/R@5/R@10 | [0.862, 0.93, 0.958] | full-haystack | 500 | 2026-09-17 | ✅ | `db87551471f90029` |
| `lme_s_full500_retrieval_20260911` | R@1/R@5/R@10 | [0.862, 0.93, 0.958] | full-haystack | 500 | 2026-09-11 | ✅ | `a85dc5422d2dd262` |
| `docs_corpus_auto_topup_off_20260916` | R@10 | 0.2833 | full-haystack | 120 | 2026-09-16 | ❌ | `c9695917a3624448` |
| `docs_corpus_auto_topup_on_20260916` | R@10 | 0.5 | full-haystack | 120 | 2026-09-16 | ❌ | `c9695917a3624448` |
| `docs_corpus_hybrid_20260916` | R@10 | 0.35 | full-haystack | 20 | 2026-09-16 | ❌ | `8293dacb888fc9cf` |
| `docs_corpus_hybrid_20260930` | R@10 | 0.75 | full-haystack | 20 | 2026-09-30 | ❌ | `cf5823f6dd042262` |
| `docs_corpus_hybrid_scoped_20260916` | R@10 | 0.5 | full-haystack | 20 | 2026-09-16 | ❌ | `8293dacb888fc9cf` |
| `docs_corpus_hybrid_scoped_20260930` | R@10 | 0.95 | full-haystack | 20 | 2026-09-30 | ❌ | `cf5823f6dd042262` |
| `docs_corpus_lexical_body_20260916` | R@10 | 1.0 | full-haystack | 20 | 2026-09-16 | ❌ | `8293dacb888fc9cf` |
| `docs_corpus_lexical_body_20260930` | R@10 | 1.0 | full-haystack | 20 | 2026-09-30 | ❌ | `cf5823f6dd042262` |
| `docs_corpus_lexical_titles_20260916` | R@10 | 1.0 | full-haystack | 20 | 2026-09-16 | ❌ | `8293dacb888fc9cf` |
| `docs_corpus_lexical_titles_20260930` | R@10 | 1.0 | full-haystack | 20 | 2026-09-30 | ❌ | `cf5823f6dd042262` |
| `lme_capacity_20260914` | R@1/R@5 by corpus budget | {"1.5M_chars": [1.0, 1.0], "3M_chars": [1.0, 1.0], "6M_chars": [0.5833, 1.0]} | full-haystack | 3 | 2026-09-14 | ❌ | `75b23053e40b7499` |
| `lme_fullctx_delta_smoke6_20260924` | AnswerAcc Δ（本臂 − full-context 基线） | {"answer_acc": 0.8333, "full_context_baseline": 0.3333, "delta": 0.5} | full-haystack | 6 | 2026-09-24 | ❌ | `7eb96c2595bf15a5` |
| `lme_oracle_bal102_routed_20260919` | AnswerAcc | 0.6569 | oracle | 102 | 2026-09-19 | ❌ | `09844dafec27496f` |
| `lme_oracle_smoke5_caliber_20260919` | AnswerAcc | 0.8 | oracle | 5 | 2026-09-19 | ❌ | `4477b6bdea7d209c` |
| `lme_s_oracle500_qa_v5_20260903` | AnswerAcc | 0.644 | oracle | 500 | 2026-09-03 | ❌ | `e9d219048302f6eb` |
| `lme_s_oracle500_retrieval_20260902` | R@1/R@3/R@5/R@10 | [1.0, 1.0, 1.0, 1.0] | oracle | 500 | 2026-09-02 | ❌ | `9404b619c986b7db` |
| `lme_smoke6_efficiency_20260924` | 每次查询的上下文成本（字符）+ 压缩比 + 每题延迟/成本（**与同配对答案面并列**，MemBench 效率口径） | {"qa_prompt_chars_mean": 3495.7, "baseline_qa_prompt_chars_mean": 57534.0, "context_compression_x": 16.46, "context_reduction_pct": 93.9, "latency_s_per_q": 38.1, "baseline_latency_s_per_q": 1.8, "latency_x_slower": 21.0, "est_cost_usd_per_q": 0.0011, "answer_acc": 0.8333, "baseline_answer_acc": 0.3333} | full-haystack | 6 | 2026-09-24 | ❌ | `7eb96c2595bf15a5` |

## 2. 重跑方式（逐条）

### `lme_s_full120_adapter_20260913`

```
python benchmark/official_lm_eval.py --limit 120 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval adapter --out output/lme_dual_adapter_20260913.json
```

- 来源：`dsh-ops/evidence/lme_dual_adapter_20260913.json`（SHA-256 `64c66b279b125f58f8f4b2ac2dcf4d66ad31fbad66ec3f2f992c114cd0651910`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：推定项：`--retrieval adapter` 由条目 id 推定（产物内未记录）。本条是 adapter 臂。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：约 $0.13（产物 est_cost_usd）+ 约 65 分钟（elapsed_s=3885）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `lme_s_full120_hybrid_20260914`

```
python benchmark/official_lm_eval.py --limit 120 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval hybrid --out output/lme_dual_hybrid_fix_20260913.json
```

- 来源：`dsh-ops/evidence/lme_dual_hybrid_fix_20260913.json`（SHA-256 `8e2838cb9a815caade71d62b662708410d3e79ff89bb692ea777b1e6ed2904a0`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：推定项（**最强的一条**）：`--retrieval hybrid` 由条目 id 推定，产物内**未记录**；该产物 timestamp=2026-09-14T01:44 与文件名 `..._20260913` 不一致（跨零点运行），外部方按日期检索文件时须注意。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：约 $0.13（产物 est_cost_usd）+ 约 72 分钟（elapsed_s=4335）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `lme_s_full120_qa_20260911`

```
python benchmark/official_lm_eval.py --limit 120 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval adapter --out output/lme_full_balanced120_20260911.json
```

- 来源：`dsh-ops/evidence/lme_full_balanced120_20260911.json`（SHA-256 `9d450f38a2c87a4743e321e3d3db0219fa1ae9b4787a636286c891e496a7e6e3`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：与上一条同一份产物（120q 的检索与答题指标出自同一次运行）。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：同上（同一次运行）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `lme_s_full120_retrieval_20260911`

```
python benchmark/official_lm_eval.py --limit 120 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval adapter --out output/lme_full_balanced120_20260911.json
```

- 来源：`dsh-ops/evidence/lme_full_balanced120_20260911.json`（SHA-256 `9d450f38a2c87a4743e321e3d3db0219fa1ae9b4787a636286c891e496a7e6e3`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：推定项：`--retrieval adapter` 同上由条目 id 推定；`--limit 120` 取产物 questions 字段。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：约 $0.13（产物 est_cost_usd）+ 约 69 分钟（elapsed_s=4156）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `lme_s_full500_qa_20260911`

```
python benchmark/official_lm_eval.py --limit 500 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval adapter --out output/lme_full500_20260911.json
```

- 来源：`dsh-ops/evidence/lme_full500_20260911.json`（SHA-256 `a85dc5422d2dd2629ad5cb9f337dbfebdff33aac0661ed3e4e4cd3f466325392`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：与上一条**同一份产物**（AnswerAcc 0.612 与 R@k 0.862/0.930/0.958 出自同一次运行）——登记拆成两条是为了分列检索与答题两个指标，重跑命令因此相同。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：同上（同一次运行，不重复计费）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `lme_s_full500_refresh_qa_20260917`

```
python benchmark/official_lm_eval.py --limit 500 --answer --strategy routed --retrieval adapter --dataset benchmark/data/longmemeval_s_cleaned.json --out p1_3_lme500_refresh_20260917.json
```

- 来源：`dsh-ops/evidence/lme_refresh_20260917.json`（SHA-256 `db87551471f9002912e228168a91de064f2654ce018320de513b74a1efdf016c`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：与上一条**同一份产物**（同一次运行）⇒ 重跑命令相同。
- 成本：约 $0.6076（同上，不重复计费）
- 耗时：elapsed_s=18291.4

### `lme_s_full500_refresh_retrieval_20260917`

```
python benchmark/official_lm_eval.py --limit 500 --answer --strategy routed --retrieval adapter --dataset benchmark/data/longmemeval_s_cleaned.json --out p1_3_lme500_refresh_20260917.json
```

- 来源：`dsh-ops/evidence/lme_refresh_20260917.json`（SHA-256 `db87551471f9002912e228168a91de064f2654ce018320de513b74a1efdf016c`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：命令与 SCORES.json 里 `lme_s_full500_retrieval_20260911` **逐字相同**（recorded，本机实测），故两行同口径。
- 成本：约 $0.6076（产物 est_cost_usd）
- 耗时：elapsed_s=18291.4（产物内字段）

### `lme_s_full500_retrieval_20260911`

```
python benchmark/official_lm_eval.py --limit 500 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --retrieval adapter --out output/lme_full500_20260911.json
```

- 来源：`dsh-ops/evidence/lme_full500_20260911.json`（SHA-256 `a85dc5422d2dd2629ad5cb9f337dbfebdff33aac0661ed3e4e4cd3f466325392`）
- 命令来源：`reconstructed_from_artifact`；验证级别：**`smoke`**
- ⚠️ 推定说明：推定项：`--retrieval adapter` 由**条目 id** 推定（产物内**未记录** retrieval 模式；同期 hybrid 条目的产物字段与之完全相同，只有数值不同）。top-k 未写死：该产物同时登记 R@1/3/5/10，对应 harness 默认 top-k=10。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身，SHA-256 校验无从做起）。
- 成本：约 $0.62（产物 est_cost_usd）+ 约 4.8 小时（产物 elapsed_s=17392）
- 耗时：见产物 elapsed_s 字段（外部方可直接读）

### `docs_corpus_auto_topup_off_20260916`

```
python dsh-ops/_v3_topup_ab.py --arms off,on --top-k 10 --golden eval/doc_golden_set_auto.json
```

- 来源：`dsh-ops/evidence/p0_v5_golden_auto_ab.txt`（SHA-256 `c9695917a3624448d6a4207aa7ac38febd923023ad713d125280456655aa7019`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑（n=120）；topup=off（旧行为）：空结果 65/120、平均返回 1.85/10
- 成本：无 LLM 调用，纯检索；两臂合计约 6 分钟

### `docs_corpus_auto_topup_on_20260916`

```
python dsh-ops/_v3_topup_ab.py --arms off,on --top-k 10 --golden eval/doc_golden_set_auto.json
```

- 来源：`dsh-ops/evidence/p0_v5_golden_auto_ab.txt`（SHA-256 `c9695917a3624448d6a4207aa7ac38febd923023ad713d125280456655aa7019`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑（n=120）；topup=on（默认）：空结果 1/120、平均返回 9.25/10、ΔR@10=+0.2167。【⚠️ 已撤回（2026-10-05）】该 +0.2167 的前提已不成立：原始证据（source_sha256 c9695917…）里 off 臂平均只返回 1.85/10、65/120 查询为空，而 2026-10-05 用逐字相同的命令重跑，off 臂返回 7.88/10、仅 1/120 空 ⇒ 补齐对检索质量的实际贡献实测为 0.0000（n=120 与 n=20 两套题集，R@1/R@5/R@10/nDCG@5 全部零差异）；新增收益/代价分解：gained_queries=0、diluted_entries=130。登记见本文件 superseded_claims（pattern 0.2167）。
- 成本：无 LLM 调用，纯检索；两臂合计约 6 分钟

### `docs_corpus_hybrid_20260916`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10 --ratchet
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_baseline_20260916.json`（SHA-256 `8293dacb888fc9cf4d0920b445dd581b5d7e8143e3267bffb845dc94ff3348f7`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑过（17:33 与 17:31 两次）；四臂含义见 arm 字段。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身）。
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_hybrid_20260930`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10   # 需 TRINITY_EVAL_SOURCE=sqlite 或 PG 可达
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_20260930.json`（SHA-256 `cf5823f6dd042262fefc0644b14a4d7ca7f4a8c0e264c3a63d7480519866ef40`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_hybrid_scoped_20260916`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10 --ratchet
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_baseline_20260916.json`（SHA-256 `8293dacb888fc9cf4d0920b445dd581b5d7e8143e3267bffb845dc94ff3348f7`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑过（17:33 与 17:31 两次）；四臂含义见 arm 字段。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身）。
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_hybrid_scoped_20260930`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10   # 需 TRINITY_EVAL_SOURCE=sqlite 或 PG 可达
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_20260930.json`（SHA-256 `cf5823f6dd042262fefc0644b14a4d7ca7f4a8c0e264c3a63d7480519866ef40`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_lexical_body_20260916`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10 --ratchet
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_baseline_20260916.json`（SHA-256 `8293dacb888fc9cf4d0920b445dd581b5d7e8143e3267bffb845dc94ff3348f7`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑过（17:33 与 17:31 两次）；四臂含义见 arm 字段。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身）。
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_lexical_body_20260930`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10   # 需 TRINITY_EVAL_SOURCE=sqlite 或 PG 可达
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_20260930.json`（SHA-256 `cf5823f6dd042262fefc0644b14a4d7ca7f4a8c0e264c3a63d7480519866ef40`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_lexical_titles_20260916`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10 --ratchet
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_baseline_20260916.json`（SHA-256 `8293dacb888fc9cf4d0920b445dd581b5d7e8143e3267bffb845dc94ff3348f7`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- ⚠️ 推定说明：本轮真跑过（17:33 与 17:31 两次）；四臂含义见 arm 字段。 产物已从被 gitignore 的 output/ 复制到 dsh-ops/evidence/（否则外部方拿不到文件本身）。
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `docs_corpus_lexical_titles_20260930`

```
python scripts/doc_retrieval_eval.py --arms A,A2,B1,B2 --top-k 10   # 需 TRINITY_EVAL_SOURCE=sqlite 或 PG 可达
```

- 来源：`dsh-ops/evidence/doc_retrieval_eval_20260930.json`（SHA-256 `cf5823f6dd042262fefc0644b14a4d7ca7f4a8c0e264c3a63d7480519866ef40`）
- 命令来源：`recorded`；验证级别：**`rerun`**
- 成本：无 LLM 调用，纯检索；单次约 1 分钟

### `lme_fullctx_delta_smoke6_20260924`

```
python benchmark/official_lm_eval.py --retrieval fullcontext --per-type 1 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --out output/lme_fullctx_smoke_20260924.json  && python benchmark/official_lm_eval.py --retrieval adapter --per-type 1 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --full-context-baseline output/lme_fullctx_smoke_20260924.json --out output/lme_adapter_smoke_20260924.json
```

- 来源：`dsh-ops/evidence/lme_adapter_smoke_20260924.json`（SHA-256 `7eb96c2595bf15a56492cadd38b9aacbc28282ffbb944bd86424d5346d6954f1`）
- 命令来源：`recorded`；验证级别：**`ran`**
- 成本：两臂合计 est_cost_usd≈0.0066（产物字段），耗时≈20s+数分钟

### `lme_oracle_bal102_routed_20260919`

```
python benchmark/official_lm_eval.py --replay output/r4_oracle102_asmoff.json.detail.jsonl --strategy routed --out output/r4_102_replay_routed.json
```

- 来源：`dsh-ops/evidence/p914_r4_102_replay_routed.json`（SHA-256 `09844dafec27496f4431b8e7443d2a36911c9a5d4d05f4e30660b3266981b02c`）
- 命令来源：`recorded`；验证级别：**`ran`**

### `lme_oracle_smoke5_caliber_20260919`

```
python benchmark/official_lm_eval.py --limit 5 --answer --out output/lme_caliber_smoke_20260919.json
```

- 来源：`dsh-ops/evidence/lme_caliber_smoke_20260919.json`（SHA-256 `4477b6bdea7d209c48d811aaefda612714000f6803f54fa549f7665ab3c0a1c5`）
- 命令来源：`recorded`；验证级别：**`ran`**

### `lme_smoke6_efficiency_20260924`

```
python benchmark/official_lm_eval.py --retrieval fullcontext --per-type 1 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --out output/lme_fullctx_smoke_20260924.json  && python benchmark/official_lm_eval.py --retrieval adapter --per-type 1 --answer --dataset benchmark/data/longmemeval_s_cleaned.json --strategy routed --full-context-baseline output/lme_fullctx_smoke_20260924.json --out output/lme_adapter_smoke_20260924.json
```

- 来源：`dsh-ops/evidence/lme_adapter_smoke_20260924.json`（SHA-256 `7eb96c2595bf15a56492cadd38b9aacbc28282ffbb944bd86424d5346d6954f1`）
- 命令来源：`recorded`；验证级别：**`ran`**

## 3. 能证 / 不能证（引用前必读）

**外部方能证的**：

1. 产物文件的内容与登记的 SHA-256 一致（`sha256sum` 自查即可）；
2. 按上面的命令重跑，得到与登记同口径的数字（协议/n/数据集写在同一行）；
3. 命令是否真的存在、参数是否被 harness 接受（`--help` 级冒烟）。

**外部方不能证的**（本文件不声称）：

1. `command_verified: smoke` 只证明**命令可解析**，**不证明**该命令就是当初产生这份产物的那条命令（`reconstructed_from_artifact` 的条目尤其如此，推定项见各条 command_note）；
2. 重跑会得到**同分布但不必逐位相同**的数字（LLM 采样、并发、数据在长）；
3. 本包不含任何第三方独立复跑结果 —— 截至目前**还没有**外部方跑过 Trinity 的数字；
4. 非 claimable 条目（oracle / mock / 合成 / n<30）**不得**被引用为对外成绩。

