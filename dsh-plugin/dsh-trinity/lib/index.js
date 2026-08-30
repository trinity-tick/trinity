/**
 * @deepseek-ai/dsh-trinity — 原生 Trinity 引擎集成插件（F2，融合核心）
 *
 * 取代 mcp-trinity（MCP 协议层）：本插件直接 spawn trinity_engine_worker.py，
 * 经 stdio NDJSON 与 Trinity 引擎直连，注册原生工具 trinity_*（无 mcp 前缀）。
 *
 * 生命周期：apply 时 spawn worker + 注册工具；dispose 时关闭 worker 并注销工具。
 * 重连：worker 意外退出时以指数退避自动重启（与 dsh-mcp-client 同策略）。
 *
 * @module @deepseek-ai/dsh-trinity
 */
import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { scrubbedParentEnv } from "@deepseek-ai/dsh-subprocess";

/** Cordis 插件名（loader 诊断用）。 */
const name = "trinity";
/** 本插件依赖的工具注册服务。 */
const inject = ["tools"];

/** 默认工具调用超时（worker 直连远快于 MCP 桥，60s 兜底足够）。 */
const DEFAULT_TOOL_CALL_TIMEOUT_MS = 60_000;
/** worker 默认路径（trinity 仓库 trinity/engine_worker.py）。 */
const DEFAULT_WORKER_PATH = "C:\\Users\\Administrator\\trinity\\trinity\\engine_worker.py";
/** 默认系统 Python（api/mcp/collector 统一解释器）。 */
const DEFAULT_PYTHON_PATH = "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python314\\python.exe";
/** 重连策略（与 dsh-mcp-client 对齐）。 */
const RECONNECT_DEFAULTS = Object.freeze({
	enabled: true,
	initialDelayMs: 500,
	maxDelayMs: 30_000,
	maxAttempts: 10
});

const Reconnect = z.object({
	enabled: z.boolean().default(RECONNECT_DEFAULTS.enabled),
	initialDelayMs: z.number().min(1).default(RECONNECT_DEFAULTS.initialDelayMs),
	maxDelayMs: z.number().min(1).default(RECONNECT_DEFAULTS.maxDelayMs),
	maxAttempts: z.number().step(1).min(1).default(RECONNECT_DEFAULTS.maxAttempts)
});

const StructureSync = z.object({
	enabled: z.boolean().default(true)
});

const Config = z.object({
	workerPath: z.string().default(DEFAULT_WORKER_PATH),
	pythonPath: z.string().default(DEFAULT_PYTHON_PATH),
	toolCallTimeoutMs: z.number().default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
	reconnect: Reconnect,
	structureSync: StructureSync
});

function resolveReconnect(config) {
	const c = config?.reconnect ?? {};
	return {
		enabled: c.enabled ?? RECONNECT_DEFAULTS.enabled,
		initialDelayMs: c.initialDelayMs ?? RECONNECT_DEFAULTS.initialDelayMs,
		maxDelayMs: c.maxDelayMs ?? RECONNECT_DEFAULTS.maxDelayMs,
		maxAttempts: c.maxAttempts ?? RECONNECT_DEFAULTS.maxAttempts
	};
}

/**
 * 管理一个 worker 子进程：spawn、NDJSON 调用、崩溃自动重启。
 */
function createWorker(config) {
	const policy = resolveReconnect(config);
	const pythonPath = config?.pythonPath ?? DEFAULT_PYTHON_PATH;
	const workerPath = config?.workerPath ?? DEFAULT_WORKER_PATH;
	const toolCallTimeoutMs = config?.toolCallTimeoutMs ?? DEFAULT_TOOL_CALL_TIMEOUT_MS;
	let child = null;
	let seq = 0;
	const pending = new Map(); // id -> {resolve, reject, timer}
	let disposed = false;
	let failedAttempts = 0;
	let reconnectTimer = null;

	function start() {
		if (disposed) return;
		child = spawn(pythonPath, [workerPath], {
			stdio: ["pipe", "pipe", "pipe"],
			// 2026-08-17: 禁用 import 期聚合器自举（trinity/__init__ ensure_bootstrapped
			// 会创建共享 MemoryAggregator 并启动 agg-ann-prewarm——真实大库 11k+ 条 faiss
			// 构建数分钟，GIL 饥饿把 worker 主循环拖死，ping/write 排队超时）。
			// worker 只需引擎功能，聚合器由 rl_feedback 等按需懒创建。
			env: { ...scrubbedParentEnv(), TRINITY_MEMORY_ENABLED: "0" },
			windowsHide: true
		});
		// 引擎日志经 stderr 转发（不 inherit，避免污染调用方 stderr/协议）
		const errChunks = [];
		child.stderr.on("data", (chunk) => {
			errChunks.push(chunk);
			if (errChunks.length > 200) errChunks.shift(); // 只留最近 200 块
		});
		const rl = createInterface({ input: child.stdout });
		rl.on("line", (line) => {
			let msg;
			try {
				msg = JSON.parse(line);
			} catch {
				return;
			}
			const entry = pending.get(msg.id);
			if (!entry) return;
			pending.delete(msg.id);
			clearTimeout(entry.timer);
			if (msg.error) {
				entry.reject(new Error(msg.error.message || "trinity worker error"));
			} else {
				entry.resolve(msg.result);
			}
		});
		child.on("error", (err) => {
			// spawn 失败也走重启逻辑
			child = null;
			rejectAll(`worker error: ${err.message}`);
			scheduleReconnect();
		});
		child.on("exit", (code) => {
			child = null;
			rejectAll(`worker exited (code=${code})`);
			scheduleReconnect();
		});
	}

	function rejectAll(reason) {
		for (const [id, entry] of pending) {
			clearTimeout(entry.timer);
			entry.reject(new Error(reason));
		}
		pending.clear();
	}

	function scheduleReconnect() {
		if (disposed || !policy.enabled) return;
		failedAttempts += 1;
		if (failedAttempts > policy.maxAttempts) {
			// 放弃：工具仍注册，调用时报错
			return;
		}
		const delay = Math.min(policy.maxDelayMs, policy.initialDelayMs * 2 ** (failedAttempts - 1));
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			failedAttempts = 0;
			start();
		}, delay);
		reconnectTimer.unref?.();
	}

	function call(method, params) {
		return new Promise((resolve, reject) => {
			if (!child || !child.stdin) {
				reject(new Error("trinity worker is not running"));
				return;
			}
			const id = ++seq;
			const timer = setTimeout(() => {
				pending.delete(id);
				reject(new Error(`trinity_${method} timed out after ${toolCallTimeoutMs}ms`));
				// 自愈（2026-08-17）: 工具调用超时说明 worker 主循环可能被阻塞
				// （如 SQLite 写锁等待叠加 >60s，busy_timeout=15s × 多步写入）。
				// 杀掉 worker → 触发 exit → rejectAll + scheduleReconnect，
				// 下次调用自动拉起新 worker，避免"活着的僵尸 worker"持续吞掉所有调用。
				if (child && child.stdin && !child.stdin.destroyed) {
					try { child.kill(); } catch {}
				}
			}, toolCallTimeoutMs);
			timer.unref?.();
			pending.set(id, { resolve, reject, timer });
			child.stdin.write(JSON.stringify({ id, method, params: params ?? {} }) + "\n");
		});
	}

	function dispose() {
		disposed = true;
		if (reconnectTimer) clearTimeout(reconnectTimer);
		rejectAll("worker disposed");
		if (child) {
			try {
				child.kill();
			} catch {}
			child = null;
		}
	}

	start();
	return { call, dispose };
}

/**
 * 生成一个 trinity_* 工具定义。
 */
function tool(name, description, parameters, outputSchema, executeImpl) {
	return defineTool({
		name,
		description,
		parameters,
		output: {
			schema: outputSchema,
			render: (_args, value) => [{
				type: "text",
				text: typeof value === "string" ? value : JSON.stringify(value, null, 2)
			}]
		},
		execute(args, exec) {
			return executeImpl(args, exec);
		}
	});
}

/**
 * F4：从 DSH 执行上下文提取会话身份（agent_id / session_id）。
 * agent_id = `dsh-<sessionId>`，使每个 DSH 会话天然成为 Trinity 独立身份，
 * 记忆写入/检索自动归属当前会话（多会话隔离）。
 */
function sessionIdentity(exec) {
	const sessionId = exec?.agent?.session?.id ?? exec?.agent?.sessionId ?? exec?.sessionId;
	if (!sessionId) return null;
	return {
		agentId: `dsh-${String(sessionId)}`,
		sessionId: String(sessionId)
	};
}

/**
 * 注册 10 个原生 trinity_* 工具（与 worker 方法对齐）。
 */
function registerTools(ctx, worker) {
	const jsonSchema = {
		type: "object",
		// 输出 schema：允许任意返回字段（trinity_* 工具返回结构各异：
		// diagnostics 的 adapter/engine/version、search 的 results、ping 的 pong/ts 等），
		// 严禁 additionalProperties:false + 空 properties —— 会把所有有数据的返回全部拒掉。
		additionalProperties: true,
		properties: {}
	};

	// F4：首次写/搜前自动注册当前 DSH 会话为 Trinity 身份（幂等）
	const identityCache = new Set();
	function ensureIdentity(exec) {
		const ident = sessionIdentity(exec);
		if (!ident || identityCache.has(ident.agentId)) return ident;
		identityCache.add(ident.agentId);
		// 异步注册，不阻塞主调用；失败仅降级（下次调用重试）
		worker.call("identity_register", { agent_id: ident.agentId, name: ident.agentId })
			.then(() => {}, () => identityCache.delete(ident.agentId));
		return ident;
	}

	const tools = [
		tool("trinity_ping", "Ping the Trinity engine worker. Returns pong with timestamp.",
			{}, jsonSchema, () => worker.call("ping", {})),

		tool("trinity_chat", "Chat with the Trinity cognitive agent: memory-injected dialogue with metacognition (confidence/gaps). Uses the full memory loop as context. Requires trinity-api (:8001) online.",
			{
				message: { type: "string", required: true, description: "Your message to Trinity." },
				session_id: { type: "string", description: "Optional session id (defaults to current DSH session)." }
			},
			jsonSchema,
			async (a, exec) => {
				// 105.22：认知对话走 API :8001（/cognition/chat 已验证 8-9s 稳定；
				// worker 内直连 dialogue 依赖过重易卡，HTTP 是可靠路径）。
				const ident = sessionIdentity(exec);
				const res = await fetch("http://127.0.0.1:8001/cognition/chat", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						message: a.message,
						session_id: a.session_id || (ident?.sessionId || "default")
					})
				});
				if (!res.ok) return { error: "trinity-api chat failed: " + res.status };
				return await res.json();
			}),

		tool("trinity_search", "Search Trinity memory (47-channel engine). Supports hybrid/semantic/graph/exact modes.",
			{
				query: { type: "string", required: true, description: "Search query string." },
				top_k: { type: "integer", description: "Number of results (default 5)." },
				mode: { type: "string", enum: ["hybrid", "semantic", "graph", "exact"], description: "Retrieval mode (default hybrid)." },
				persona_id: { type: "string", description: "Filter by persona." },
				agent_id: { type: "string", description: "Filter by agent (namespace isolation)." },
				session_id: { type: "string", description: "Filter by session." }
			}, jsonSchema, async (a, exec) => {
				const ident = ensureIdentity(exec);
				// 未显式指定时自动归属当前会话（F4）；空结果时自动回退全局检索，
				// 避免"当前会话无记忆 → 历史记忆永远搜不到"的体验断裂。
				const autoScope = !a.agent_id && ident ? { agent_id: ident.agentId, session_id: ident.sessionId } : {};
				const r1 = await worker.call("search", { ...a, ...autoScope });
				const results = Array.isArray(r1) ? r1 : (r1?.results ?? []);
				if (results.length > 0 || a.agent_id || !ident) return r1;
				const r2 = await worker.call("search", { ...a, agent_id: undefined, session_id: undefined, persona_id: undefined, tenant_id: undefined });
				const g = Array.isArray(r2) ? r2 : (r2?.results ?? []);
				return {
					...(r2 ?? {}),
					results: g,
					fallback: { from: "global", reason: "session-scoped search returned no results", scoped: results.length, global: g.length }
				};
			}),

		tool("trinity_write", "Write memory to Trinity (CRDT versioned, SHA-256 audited).",
			{
				content: { type: "string", required: true, description: "Memory text content." },
				metadata: { type: "object", additionalProperties: true, description: "Additional metadata dict." },
				category: { type: "string", description: "Memory category (default general)." },
				tags: { type: "array", items: { type: "string" }, description: "List of tags." },
				importance: { type: "number", description: "Importance 0-1 (default 0.5)." },
				agent_id: { type: "string", description: "Agent namespace (auto-injected from DSH session when omitted)." },
				session_id: { type: "string", description: "Session id (auto-injected from DSH session when omitted)." }
			}, jsonSchema, (a, exec) => {
				const ident = ensureIdentity(exec);
				return worker.call("write", {
					...a,
					// F4：显式身份参数（未显式指定时自动归属当前会话）
					...(a.agent_id ? {} : ident ? { agent_id: ident.agentId, session_id: ident.sessionId } : {})
				});
			}),

		tool("trinity_update", "Update Trinity memory (conflict-preserving, version+1).",
			{
				memory_id: { type: "string", required: true, description: "Target memory ID." },
				new_content: { type: "string", required: true, description: "New content text." }
			}, jsonSchema, (a) => worker.call("update", a)),

		tool("trinity_delete", "Soft-delete Trinity memory (audit chain preserved).",
			{
				memory_id: { type: "string", required: true, description: "Target memory ID." }
			}, jsonSchema, (a) => worker.call("delete", a)),

		tool("trinity_audit", "Query SHA-256 provenance/version chain for a memory entry.",
			{
				memory_id: { type: "string", required: true, description: "Target memory ID." }
			}, jsonSchema, (a) => worker.call("audit", a)),

		tool("trinity_diagnostics", "Run full Trinity engine diagnostics (version, storage, channels, counts).",
			{}, jsonSchema, () => worker.call("diagnostics", {})),

		tool("trinity_chronicle", "Record a sequence of events (journal-style chronicle) to session history.",
			{
				events: { type: "array", required: true, items: { type: "object", additionalProperties: true }, description: "Event list, each with role/content/metadata." },
				title: { type: "string", description: "Optional entry title." },
				session_id: { type: "string", description: "Optional target session ID." }
			}, jsonSchema, (a) => worker.call("chronicle", a)),

		tool("trinity_tag_search", "Search memories/sessions by tags (OR logic).",
			{
				tags: { type: "array", required: true, items: { type: "string" }, description: "Tags to search (OR match)." },
				top_k: { type: "integer", description: "Max results (default 10)." },
				session_id: { type: "string", description: "Limit to one session." }
			}, jsonSchema, (a) => worker.call("tag_search", a)),

		tool("trinity_identity_register", "Register the current DSH session as a Trinity identity (agent anchor).",
			{
				agent_id: { type: "string", required: true, description: "Agent ID (e.g. dsh-<session>)." },
				name: { type: "string", description: "Display name (default agent_id)." }
			}, jsonSchema, (a) => worker.call("identity_register", a)),

		// ── 结构层工具（DSH 结构已原生承载于 Trinity）──
		tool("trinity_trajectory", "Query the DSH session event stream stored in Trinity (replayable trajectory: turns, messages, tool calls, results).",
			{
				session_id: { type: "string", description: "Filter by DSH session id." },
				type: { type: "string", enum: ["turn/start", "turn/end", "user/message", "assistant/message", "tool/call", "tool/result", "todo/write", "request/header", "goal/write", "schedule/create", "compacted_turn"], description: "Filter by event type." },
				agent_id: { type: "string", description: "Filter by agent (dsh-<session>)." },
				limit: { type: "integer", description: "Max events (default 200, max 2000)." }
			}, jsonSchema, (a) => worker.call("structure_query", a)),

		tool("trinity_sessions", "List DSH sessions whose structure is stored in Trinity.",
			{}, jsonSchema, () => worker.call("structure_sessions", {})),

		tool("trinity_structure_stats", "Stats of the DSH structure layer in Trinity (sessions/events/goals/todos/headers).",
			{}, jsonSchema, () => worker.call("structure_stats", {})),

		tool("trinity_goal", "Track a long-running objective as a structured goal in Trinity (status/phase/round).",
			{
				goal_id: { type: "string", required: true, description: "Stable goal id." },
				objective: { type: "string", description: "Objective text." },
				status: { type: "string", enum: ["active", "paused", "completed", "blocked"], description: "Goal status." },
				phase: { type: "string", description: "Current phase." },
				round: { type: "integer", description: "Completed rounds." },
				max_rounds: { type: "integer", description: "Round cap." }
			}, jsonSchema, (a) => worker.call("goal_upsert", a)),

		tool("trinity_goals", "List tracked goals in Trinity.",
			{}, jsonSchema, () => worker.call("goal_list", {})),

		tool("trinity_schedule", "Track a DSH session schedule (timed reminder) as structured data in Trinity.",
			{
				schedule_id: { type: "string", required: true, description: "Stable schedule id." },
				prompt: { type: "string", required: true, description: "Reminder prompt content." },
				target: { type: "string", description: "Target time / interval description." },
				status: { type: "string", enum: ["active", "completed", "deleted"], description: "Schedule status." }
			}, jsonSchema, (a) => worker.call("schedule_upsert", a)),

		tool("trinity_schedules", "List tracked schedules in Trinity.",
			{}, jsonSchema, () => worker.call("schedule_list", {})),

		tool("trinity_rl_feedback", "RL memory feedback — record user confirm/correction to update Q-value (MemRL: retrieval-use-feedback loop).",
			{
				memory_id: { type: "string", required: true, description: "Target memory ID (pool or engine side)." },
				positive: { type: "boolean", description: "True=confirm/success (raise Q), False=correct/failure (lower Q). Default true." }
			}, jsonSchema, (a) => worker.call("rl_feedback", a)),

		tool("trinity_reason", "Open-domain reasoning QA (RouteReasoner: verified strategies — turn-granularity for multi-session, REL+inner2 for temporal, two-stage for preference).",
			{
				query: { type: "string", required: true, description: "Question to answer." },
				qtype: { type: "string", description: "Question-type hint for strategy routing (multi-session/temporal-reasoning/single-session-preference/...)." },
				question_date: { type: "string", description: "Question date YYYY/MM/DD (temporal REL computation)." },
				top_k: { type: "integer", description: "Evidence top-k (default 8)." },
				agent_id: { type: "string", description: "Filter evidence by agent." },
				persona_id: { type: "string", description: "Filter evidence by persona." }
			}, jsonSchema, (a) => worker.call("reason", a))
	];

	for (const definition of tools) {
		ctx.tools.register(definition);
	}
}

/**
 * Cordis 插件入口：spawn worker + 注册 trinity_* 工具 + 订阅 DSH 会话结构事件。
 *
 * 结构融合（DSH 结构 → Trinity 原生承载）：
 *   - session/created → 自动注册 Trinity 身份（agent_id = dsh-<sessionId>）
 *   - session/event   → 缓冲会话事件（user/assistant/tool/turn/todo/header）
 *   - session/flush   → 批量同步结构到引擎库（dsh_* 表，可查/可回放/可审计）
 *   - session/disposed→ 最终同步 + 关闭会话
 */
function apply(ctx, config) {
	const worker = createWorker(config);
	ctx.effect(() => {
		return () => worker.dispose();
	}, "trinity.worker");
	registerTools(ctx, worker);

	// ── 结构融合：DSH 会话事件流自动流入 Trinity ──────────────────
	const syncConfig = config?.structureSync ?? {};
	const enabled = syncConfig.enabled !== false;
	if (!enabled) return;

	// 每会话缓冲：{ sessionId -> { events: [], todos, headers, lastFlushAt } }
	const buffers = new Map();
	const identityCache = new Set();
	const sessionMeta = new Map(); // sessionId -> { agentId, title, parent }

	function agentIdFor(sessionId) {
		return `dsh-${sessionId}`;
	}

	function ensureIdentity(sessionId) {
		const agentId = agentIdFor(sessionId);
		if (identityCache.has(agentId)) return;
		identityCache.add(agentId);
		worker.call("identity_register", { agent_id: agentId, name: agentId })
			.then(() => {}, () => identityCache.delete(agentId));
	}

	function flushBuffer(sessionId) {
		const buf = buffers.get(sessionId);
		if (!buf) return;
		buffers.delete(sessionId);
		if (buf.events.length === 0 && buf.todos === undefined && buf.headers.length === 0) return;
		const meta = sessionMeta.get(sessionId) ?? {};
		worker.call("structure_sync", {
			session_id: sessionId,
			agent_id: meta.agentId ?? agentIdFor(sessionId),
			title: meta.title,
			parent_session: meta.parent,
			status: buf.closed ? "closed" : "active",
			events: buf.events,
			todos: buf.todos,
			headers: buf.headers
		}).then(() => {}, (err) => {
			// 失败重放缓冲（下次 flush 再试），避免丢结构
			buffers.set(sessionId, buf);
		});
	}

	function bufferFor(sessionId) {
		let buf = buffers.get(sessionId);
		if (!buf) {
			buf = { events: [], todos: undefined, headers: [], closed: false };
			buffers.set(sessionId, buf);
		}
		return buf;
	}

	// 事件 → 结构行（只保留有结构价值的类型；chunk 级噪声丢弃）
	function toStructureEvent(event) {
		const d = event.data ?? {};
		switch (event.type) {
			case "user/message": {
				const text = extractMessageText(d.message?.content ?? d.content);
				return { seq: event.seq, type: "user/message", turn: d.turn, step: d.step, time: event.time, data: { content: text, source: d.message?.source?.kind ?? "user" } };
			}
			case "assistant/message": {
				const text = extractMessageText(d.message?.content ?? d.content);
				return { seq: event.seq, type: "assistant/message", turn: d.turn, step: d.step, time: event.time, data: { content: text, provider: d.message?.source?.provider, model: d.message?.source?.model, usage: d.usage ?? null } };
			}
			case "tool/call":
				return { seq: event.seq, type: "tool/call", turn: d.turn, step: d.step, time: event.time, data: { name: d.name, callId: d.callId, arguments: String(d.arguments ?? "").slice(0, 2000) } };
			case "tool/result":
				return { seq: event.seq, type: "tool/result", turn: d.turn, step: d.step, time: event.time, data: { callId: d.message?.source?.callId ?? d.callId, error: d.error ?? null, isError: d.message?.isError ?? false } };
			case "turn/start":
				return { seq: event.seq, type: "turn/start", turn: d.turn, time: event.time, data: { turn: d.turn } };
			case "turn/end":
				return { seq: event.seq, type: "turn/end", turn: d.turn, time: event.time, data: { reason: d.reason ?? null } };
			case "todo/write":
				return { seq: event.seq, type: "todo/write", time: event.time, data: { count: (d.todos ?? []).length } };
			case "request/header":
				return { seq: event.seq, type: "request/header", time: event.time, data: { reason: d.reason } };
			case "goal/change": {
				// DSH goal 快照（事件溯源，含完整 GoalSnapshot）→ 结构事件。
				// 2026-08-15：此前缺此分支，goal 事件被静默丢弃 → 新 goal 不落库。
				const goal = d.goal ?? d;
				const gid = goal?.id ?? d.goalId ?? null;
				if (!gid) return null;
				return {
					seq: event.seq, type: "goal/write", time: event.time,
					data: {
						goal_id: gid,
						objective: goal?.objective ?? "",
						phase: goal?.phase ?? d.operation ?? "active",
						revision: goal?.revision ?? d.revision ?? 0,
						roundsStarted: d.roundsStarted ?? 0,
						createdAt: d.createdAt ?? event.time,
						updatedAt: d.updatedAt ?? event.time,
						operation: d.operation ?? "create",
					},
				};
			}
			default:
				return null; // chunk/step/seed 等噪声丢弃
		}
	}

	function extractMessageText(content) {
		if (typeof content === "string") return content.slice(0, 8000);
		if (Array.isArray(content)) {
			return content
				.filter((b) => b && (b.type === "text" || b.type === "reasoning"))
				.map((b) => b.text ?? "")
				.join("\n")
				.slice(0, 8000);
		}
		return "";
	}

	// session/created：注册身份 + 记录会话元数据
	ctx.on("session/created", (session) => {
		const sid = String(session.id);
		ensureIdentity(sid);
		sessionMeta.set(sid, {
			agentId: agentIdFor(sid),
			title: session.header?.meta?.title,
			parent: session.header?.parentSession ? String(session.header.parentSession) : undefined
		});
	});

	// session/event：缓冲结构事件
	ctx.on("session/event", (session, event) => {
		if (event.seq < session.firstLiveSeq) return; // 种子/回放事件不重复同步
		const sid = String(session.id);
		const struct = toStructureEvent(event);
		if (!struct) return;
		const buf = bufferFor(sid);
		buf.events.push(struct);
		// todo 快照直接挂在缓冲上（whole-list 最新覆盖）
		if (event.type === "todo/write") {
			buf.todos = (event.data?.todos ?? []).map((t) => ({ content: t.content, status: t.status }));
		}
		if (event.type === "request/header") {
			buf.headers.push({ seq: event.seq, reason: event.data?.reason, header: event.data?.header ?? {}, time: event.time });
		}
		// 阈值 flush：每 50 事件或 5s 兜底
		if (buf.events.length >= 50) flushBuffer(sid);
	});

	// session/flush：持久化检查点 → 同步结构
	ctx.on("session/flush", (session) => {
		flushBuffer(String(session.id));
	});

	// session/disposed：最终同步 + 标记关闭
	ctx.on("session/disposed", (session) => {
		const sid = String(session.id);
		const buf = buffers.get(sid);
		if (buf) buf.closed = true;
		flushBuffer(sid);
		sessionMeta.delete(sid);
	});
}

export { Config, apply, inject, name };
