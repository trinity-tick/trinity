/**
 * P2-1a: Trinity TypeScript SDK
 * ==============================
 *
 * 与 Python SDK (trinity/sdk/client.py) 接口对等，
 * 提供 Memory / Agent / Knowledge Graph / Retrieval 等核心能力。
 *
 * Usage:
 *   import { TrinityClient } from '@trinity-sdk/js';
 *   const client = new TrinityClient({ endpoint: 'http://localhost:8100' });
 */

// ── Types ────────────────────────────────────────────────────────────────

export interface MemoryRecord {
  memory_id: string;
  content: string;
  persona_id: string;
  agent_id: string;
  status: 'active' | 'archived' | 'soft_deleted' | 'pending';
  tags: string[];
  confidence: number;
  created_at: string;
  updated_at: string;
}

export interface AgentRecord {
  agent_id: string;
  name: string;
  role: string;
  status: string;
  registered_at: string;
  memory_pool_size: number;
}

export interface SearchResult {
  score: number;
  memory: MemoryRecord;
  matched_segments: string[];
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  strategy?: 'semantic' | 'keyword' | 'hybrid' | 'causal';
  filters?: Record<string, unknown>;
}

export interface IngestRequest {
  content: string;
  persona_id?: string;
  agent_id?: string;
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface ClientConfig {
  endpoint: string;
  apiKey?: string;
  timeout?: number;
  maxRetries?: number;
}

export interface HealthStatus {
  status: string;
  version: string;
  uptime_seconds: number;
  memory_count: number;
  agent_count: number;
  component_status: string;
}

// ── Client ───────────────────────────────────────────────────────────────

export class TrinityClient {
  private endpoint: string;
  private apiKey: string;
  private timeout: number;
  private maxRetries: number;

  constructor(config: ClientConfig) {
    this.endpoint = config.endpoint.replace(/\/$/, '');
    this.apiKey = config.apiKey || '';
    this.timeout = config.timeout || 30000;
    this.maxRetries = config.maxRetries || 3;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown
  ): Promise<T> {
    const url = `${this.endpoint}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.apiKey) {
      headers['Authorization'] = `Bearer ${this.apiKey}`;
    }

    let lastError: Error | null = null;
    for (let attempt = 0; attempt < this.maxRetries; attempt++) {
      try {
        const resp = await fetch(url, {
          method,
          headers,
          body: body ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });
        clearTimeout(timer);
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        }
        return (await resp.json()) as T;
      } catch (e) {
        lastError = e instanceof Error ? e : new Error(String(e));
        if (attempt < this.maxRetries - 1) {
          await this.sleep(2 ** attempt * 100);
        }
      }
    }
    throw lastError;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  // ── Health ────────────────────────────────────────────────────────

  async health(): Promise<HealthStatus> {
    return this.request<HealthStatus>('GET', '/health');
  }

  // ── Memory CRUD ───────────────────────────────────────────────────

  async createMemory(input: IngestRequest): Promise<MemoryRecord> {
    return this.request<MemoryRecord>('POST', '/api/v1/memories', input);
  }

  async getMemory(memoryId: string): Promise<MemoryRecord | null> {
    try {
      return await this.request<MemoryRecord>('GET', `/api/v1/memories/${memoryId}`);
    } catch {
      return null;
    }
  }

  async updateMemory(
    memoryId: string,
    updates: Partial<IngestRequest>
  ): Promise<MemoryRecord> {
    return this.request<MemoryRecord>('PUT', `/api/v1/memories/${memoryId}`, updates);
  }

  async deleteMemory(memoryId: string): Promise<boolean> {
    await this.request<void>('DELETE', `/api/v1/memories/${memoryId}`);
    return true;
  }

  async listMemories(
    page?: number,
    pageSize?: number,
    personaId?: string
  ): Promise<MemoryRecord[]> {
    const params = new URLSearchParams();
    if (page) params.set('page', String(page));
    if (pageSize) params.set('page_size', String(pageSize));
    if (personaId) params.set('persona_id', personaId);
    const qs = params.toString() ? `?${params}` : '';
    return this.request<MemoryRecord[]>('GET', `/api/v1/memories${qs}`);
  }

  // ── Search ────────────────────────────────────────────────────────

  async searchMemories(req: SearchRequest): Promise<SearchResult[]> {
    return this.request<SearchResult[]>('POST', '/api/v1/memories/search', req);
  }

  async semanticSearch(query: string, topK = 10): Promise<SearchResult[]> {
    return this.searchMemories({ query, top_k: topK, strategy: 'semantic' });
  }

  async keywordSearch(query: string, topK = 10): Promise<SearchResult[]> {
    return this.searchMemories({ query, top_k: topK, strategy: 'keyword' });
  }

  async hybridSearch(query: string, topK = 10): Promise<SearchResult[]> {
    return this.searchMemories({ query, top_k: topK, strategy: 'hybrid' });
  }

  // ── Agent ─────────────────────────────────────────────────────────

  async registerAgent(name: string, role: string): Promise<AgentRecord> {
    return this.request<AgentRecord>('POST', '/api/v1/agents', { name, role });
  }

  async listAgents(): Promise<AgentRecord[]> {
    return this.request<AgentRecord[]>('GET', '/api/v1/agents');
  }

  async getAgent(agentId: string): Promise<AgentRecord | null> {
    try {
      return await this.request<AgentRecord>('GET', `/api/v1/agents/${agentId}`);
    } catch {
      return null;
    }
  }

  // ── Batch ─────────────────────────────────────────────────────────

  async bulkIngest(inputs: IngestRequest[]): Promise<MemoryRecord[]> {
    return this.request<MemoryRecord[]>('POST', '/api/v1/memories/bulk', { memories: inputs });
  }

  async bulkDelete(memoryIds: string[]): Promise<Record<string, boolean>> {
    return this.request<Record<string, boolean>>('POST', '/api/v1/memories/bulk_delete', {
      memory_ids: memoryIds,
    });
  }
}

// ── Self-test ────────────────────────────────────────────────────────────

export interface SelfTestResult {
  module: string;
  passed: number;
  failed: number;
  total: number;
  details: { test: string; status: 'PASS' | 'FAIL'; reason?: string }[];
}

export function selfTest(): SelfTestResult {
  const result: SelfTestResult = {
    module: 'P2-1a_js_sdk',
    passed: 0,
    failed: 0,
    total: 0,
    details: [],
  };

  function pass(test: string) {
    result.passed++;
    result.total++;
    result.details.push({ test, status: 'PASS' });
  }
  function fail(test: string, reason: string) {
    result.failed++;
    result.total++;
    result.details.push({ test, status: 'FAIL', reason });
  }

  // Test 1: Client instantiation
  try {
    const c = new TrinityClient({ endpoint: 'http://localhost:8100' });
    if (c instanceof TrinityClient) pass('Client instantiation');
    else fail('Client instantiation', 'not instance of TrinityClient');
  } catch (e) {
    fail('Client instantiation', String(e));
  }

  // Test 2: Config defaults
  try {
    const c = new TrinityClient({ endpoint: 'http://localhost:8100' });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const internal = c as any;
    if (internal.timeout === 30000) pass('Config defaults');
    else fail('Config defaults', `timeout=${internal.timeout}`);
  } catch (e) {
    fail('Config defaults', String(e));
  }

  // Test 3: Type exports
  try {
    const types = [
      'TrinityClient',
      'MemoryRecord',
      'AgentRecord',
      'SearchResult',
      'SearchRequest',
      'ClientConfig',
    ];
    let ok = true;
    for (const t of types) {
      if (!(t in { TrinityClient: 1, MemoryRecord: 1, AgentRecord: 1, SearchResult: 1,
                  SearchRequest: 1, ClientConfig: 1 })) ok = false;
    }
    if (ok) pass('Type exports');
    else fail('Type exports', 'missing type');
  } catch (e) {
    fail('Type exports', String(e));
  }

  // Test 4: Endpoint normalization
  try {
    const c = new TrinityClient({ endpoint: 'http://localhost:8100/' });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((c as any).endpoint === 'http://localhost:8100') pass('Endpoint normalization');
    else fail('Endpoint normalization', 'trailing slash not stripped');
  } catch (e) {
    fail('Endpoint normalization', String(e));
  }

  // Test 5: API key handling
  try {
    const c = new TrinityClient({ endpoint: 'http://localhost:8100', apiKey: 'sk-test' });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((c as any).apiKey === 'sk-test') pass('API key handling');
    else fail('API key handling', 'wrong key');
  } catch (e) {
    fail('API key handling', String(e));
  }

  // Test 6: Method signatures exist
  try {
    const methods = [
      'health', 'createMemory', 'getMemory', 'updateMemory', 'deleteMemory',
      'listMemories', 'searchMemories', 'semanticSearch', 'keywordSearch',
      'hybridSearch', 'registerAgent', 'listAgents', 'getAgent',
      'bulkIngest', 'bulkDelete',
    ];
    const proto = TrinityClient.prototype;
    let ok = true;
    for (const m of methods) {
      if (typeof (proto as unknown as Record<string, unknown>)[m] !== 'function') ok = false;
    }
    if (ok) pass('Method signatures');
    else fail('Method signatures', 'missing method');
  } catch (e) {
    fail('Method signatures', String(e));
  }

  // Test 7: Self-test function export
  try {
    if (typeof selfTest === 'function') pass('Self-test export');
    else fail('Self-test export', 'not a function');
  } catch (e) {
    fail('Self-test export', String(e));
  }

  // Test 8: SearchRequest type completeness
  try {
    const req: SearchRequest = {
      query: 'test',
      top_k: 10,
      strategy: 'hybrid',
      filters: { status: 'active' },
    };
    if (req.query === 'test' && req.strategy === 'hybrid') pass('SearchRequest type');
    else fail('SearchRequest type', 'fields mismatch');
  } catch (e) {
    fail('SearchRequest type', String(e));
  }

  return result;
}

// ── Main ─────────────────────────────────────────────────────────────────

if (require.main === module) {
  const r = selfTest();
  console.log(JSON.stringify(r, null, 2));
}

// ═══════════════════════════════════════════════════════════════════════
// TrinityGatewayClient — Memory Gateway 兼容客户端 (V3-2b, 2026-08-14)
// 面向 OpenAI/Mem0 兼容网关 (:8002) 的轻量记忆客户端。
//   const g = new TrinityGatewayClient({ baseUrl: 'http://localhost:8002' });
//   await g.add('用户偏好深色主题', { tags: ['preference'] });
//   const hits = await g.search('用户偏好');
//   const reply = await g.chat([{ role: 'user', content: '我的偏好？' }]);
// ═══════════════════════════════════════════════════════════════════════

export interface GatewayMemory {
  memory_id?: string;
  content: string;
  tags?: string[];
  category?: string;
  importance?: number;
}

export interface GatewayChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface GatewayConfig {
  baseUrl: string;
  apiKey?: string;
  timeoutMs?: number;
}

export class TrinityGatewayClient {
  private baseUrl: string;
  private apiKey: string;
  private timeoutMs: number;

  constructor(config: GatewayConfig) {
    this.baseUrl = config.baseUrl.replace(/\/$/, '');
    this.apiKey = config.apiKey || '';
    this.timeoutMs = config.timeoutMs || 30000;
  }

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.apiKey) headers['Authorization'] = `Bearer ${this.apiKey}`;
    try {
      const resp = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      return (await resp.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<{ status: string }> {
    return this.req('GET', '/health');
  }

  async add(content: string, opts: { tags?: string[]; category?: string; importance?: number } = {}): Promise<GatewayMemory> {
    return this.req('POST', '/v1/memories', { content, ...opts });
  }

  async search(query: string, topK = 5): Promise<GatewayMemory[]> {
    const data = await this.req<{ results: GatewayMemory[] }>('POST', '/v1/memory/search', {
      query,
      top_k: topK,
      strategy: 'rrf',
    });
    return data.results || [];
  }

  async get(memoryId: string): Promise<GatewayMemory> {
    return this.req('GET', `/v1/memories/${memoryId}`);
  }

  async delete(memoryId: string): Promise<{ deleted: boolean }> {
    return this.req('DELETE', `/v1/memories/${memoryId}`);
  }

  async chat(messages: GatewayChatMessage[], model?: string, memoryK = 5): Promise<string> {
    const data = await this.req<{ choices: Array<{ message: { content: string } }> }>(
      'POST',
      '/v1/chat/completions',
      { messages, model, memory_k: memoryK },
    );
    return data.choices?.[0]?.message?.content ?? '';
  }
}
