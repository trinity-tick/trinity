/*
P2-1b: Trinity Go SDK
=====================

与 Python SDK (trinity/sdk/client.py) 接口对等，
提供 Memory / Agent / Knowledge Graph / Retrieval 等核心能力。

Usage:

	import "github.com/trinity-sdk/go"

	client := trinity.NewClient(trinity.ClientConfig{
	    Endpoint: "http://localhost:8100",
	})
*/

package trinity

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ── Types ──────────────────────────────────────────────────────────────────

// MemoryRecord 代表一条记忆记录。
type MemoryRecord struct {
	MemoryID   string   `json:"memory_id"`
	Content    string   `json:"content"`
	PersonaID  string   `json:"persona_id"`
	AgentID    string   `json:"agent_id"`
	Status     string   `json:"status"`
	Tags       []string `json:"tags"`
	Confidence float64  `json:"confidence"`
	CreatedAt  string   `json:"created_at"`
	UpdatedAt  string   `json:"updated_at"`
}

// AgentRecord 代表一个 Agent 注册记录。
type AgentRecord struct {
	AgentID        string `json:"agent_id"`
	Name           string `json:"name"`
	Role           string `json:"role"`
	Status         string `json:"status"`
	RegisteredAt   string `json:"registered_at"`
	MemoryPoolSize int    `json:"memory_pool_size"`
}

// SearchResult 搜索结果。
type SearchResult struct {
	Score           float64        `json:"score"`
	Memory          MemoryRecord   `json:"memory"`
	MatchedSegments []string       `json:"matched_segments"`
}

// SearchRequest 搜索请求体。
type SearchRequest struct {
	Query    string                 `json:"query"`
	TopK     int                    `json:"top_k,omitempty"`
	Strategy string                 `json:"strategy,omitempty"`
	Filters  map[string]interface{} `json:"filters,omitempty"`
}

// IngestRequest 记忆摄入请求。
type IngestRequest struct {
	Content   string                 `json:"content"`
	PersonaID string                 `json:"persona_id,omitempty"`
	AgentID   string                 `json:"agent_id,omitempty"`
	Tags      []string               `json:"tags,omitempty"`
	Metadata  map[string]interface{} `json:"metadata,omitempty"`
}

// HealthStatus 健康检查返回体。
type HealthStatus struct {
	Status          string  `json:"status"`
	Version         string  `json:"version"`
	UptimeSeconds   float64 `json:"uptime_seconds"`
	MemoryCount     int     `json:"memory_count"`
	AgentCount      int     `json:"agent_count"`
	ComponentStatus string  `json:"component_status"`
}

// ClientConfig 客户端配置。
type ClientConfig struct {
	Endpoint   string
	APIKey     string
	Timeout    time.Duration
	MaxRetries int
}

// ── Client ──────────────────────────────────────────────────────────────────

// Client is the Trinity Go SDK client.
type Client struct {
	endpoint   string
	apiKey     string
	timeout    time.Duration
	maxRetries int
	httpClient *http.Client
}

// NewClient creates a new Trinity client.
func NewClient(cfg ClientConfig) *Client {
	to := cfg.Timeout
	if to == 0 {
		to = 30 * time.Second
	}
	retries := cfg.MaxRetries
	if retries == 0 {
		retries = 3
	}
	return &Client{
		endpoint:   strings.TrimRight(cfg.Endpoint, "/"),
		apiKey:     cfg.APIKey,
		timeout:    to,
		maxRetries: retries,
		httpClient: &http.Client{Timeout: to},
	}
}

func (c *Client) do(method, path string, body interface{}, result interface{}) error {
	url := c.endpoint + path

	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	var lastErr error
	for attempt := 0; attempt < c.maxRetries; attempt++ {
		req, err := http.NewRequest(method, url, bodyReader)
		if err != nil {
			return fmt.Errorf("new request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		if c.apiKey != "" {
			req.Header.Set("Authorization", "Bearer "+c.apiKey)
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("http do: %w", err)
			if attempt < c.maxRetries-1 {
				time.Sleep(time.Duration(1<<attempt) * 100 * time.Millisecond)
			}
			continue
		}
		defer resp.Body.Close()

		respData, err := io.ReadAll(resp.Body)
		if err != nil {
			return fmt.Errorf("read body: %w", err)
		}

		if resp.StatusCode >= 400 {
			lastErr = fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(respData))
			if attempt < c.maxRetries-1 {
				time.Sleep(time.Duration(1<<attempt) * 100 * time.Millisecond)
			}
			continue
		}

		return json.Unmarshal(respData, result)
	}

	return lastErr
}

// ── Health ──────────────────────────────────────────────────────────────────

// Health returns the service health status.
func (c *Client) Health() (*HealthStatus, error) {
	var hs HealthStatus
	if err := c.do("GET", "/health", nil, &hs); err != nil {
		return nil, err
	}
	return &hs, nil
}

// ── Memory CRUD ─────────────────────────────────────────────────────────────

// CreateMemory creates a new memory record.
func (c *Client) CreateMemory(input *IngestRequest) (*MemoryRecord, error) {
	var m MemoryRecord
	if err := c.do("POST", "/api/v1/memories", input, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// GetMemory retrieves a memory by ID.
func (c *Client) GetMemory(memoryID string) (*MemoryRecord, error) {
	var m MemoryRecord
	if err := c.do("GET", "/api/v1/memories/"+memoryID, nil, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// UpdateMemory updates an existing memory.
func (c *Client) UpdateMemory(memoryID string, updates *IngestRequest) (*MemoryRecord, error) {
	var m MemoryRecord
	if err := c.do("PUT", "/api/v1/memories/"+memoryID, updates, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// DeleteMemory deletes a memory by ID.
func (c *Client) DeleteMemory(memoryID string) (bool, error) {
	if err := c.do("DELETE", "/api/v1/memories/"+memoryID, nil, nil); err != nil {
		return false, err
	}
	return true, nil
}

// ListMemories lists memories with optional filtering.
func (c *Client) ListMemories(page, pageSize int, personaID string) ([]MemoryRecord, error) {
	params := []string{}
	if page > 0 {
		params = append(params, fmt.Sprintf("page=%d", page))
	}
	if pageSize > 0 {
		params = append(params, fmt.Sprintf("page_size=%d", pageSize))
	}
	if personaID != "" {
		params = append(params, "persona_id="+personaID)
	}
	path := "/api/v1/memories"
	if len(params) > 0 {
		path += "?" + strings.Join(params, "&")
	}
	var mems []MemoryRecord
	if err := c.do("GET", path, nil, &mems); err != nil {
		return nil, err
	}
	return mems, nil
}

// ── Search ──────────────────────────────────────────────────────────────────

// SearchMemories performs a full search.
func (c *Client) SearchMemories(req *SearchRequest) ([]SearchResult, error) {
	var sr []SearchResult
	if err := c.do("POST", "/api/v1/memories/search", req, &sr); err != nil {
		return nil, err
	}
	return sr, nil
}

// SemanticSearch performs semantic search.
func (c *Client) SemanticSearch(query string, topK int) ([]SearchResult, error) {
	return c.SearchMemories(&SearchRequest{Query: query, TopK: topK, Strategy: "semantic"})
}

// KeywordSearch performs keyword search.
func (c *Client) KeywordSearch(query string, topK int) ([]SearchResult, error) {
	return c.SearchMemories(&SearchRequest{Query: query, TopK: topK, Strategy: "keyword"})
}

// HybridSearch performs hybrid search.
func (c *Client) HybridSearch(query string, topK int) ([]SearchResult, error) {
	return c.SearchMemories(&SearchRequest{Query: query, TopK: topK, Strategy: "hybrid"})
}

// ── Agent ───────────────────────────────────────────────────────────────────

// RegisterAgent registers a new agent.
func (c *Client) RegisterAgent(name, role string) (*AgentRecord, error) {
	body := map[string]string{"name": name, "role": role}
	var a AgentRecord
	if err := c.do("POST", "/api/v1/agents", body, &a); err != nil {
		return nil, err
	}
	return &a, nil
}

// ListAgents lists all registered agents.
func (c *Client) ListAgents() ([]AgentRecord, error) {
	var agents []AgentRecord
	if err := c.do("GET", "/api/v1/agents", nil, &agents); err != nil {
		return nil, err
	}
	return agents, nil
}

// GetAgent retrieves an agent by ID.
func (c *Client) GetAgent(agentID string) (*AgentRecord, error) {
	var a AgentRecord
	if err := c.do("GET", "/api/v1/agents/"+agentID, nil, &a); err != nil {
		return nil, err
	}
	return &a, nil
}

// ── Batch ───────────────────────────────────────────────────────────────────

// BulkIngest ingests multiple memories at once.
func (c *Client) BulkIngest(inputs []IngestRequest) ([]MemoryRecord, error) {
	body := map[string]interface{}{"memories": inputs}
	var mems []MemoryRecord
	if err := c.do("POST", "/api/v1/memories/bulk", body, &mems); err != nil {
		return nil, err
	}
	return mems, nil
}

// BulkDelete deletes multiple memories at once.
func (c *Client) BulkDelete(memoryIDs []string) (map[string]bool, error) {
	body := map[string]interface{}{"memory_ids": memoryIDs}
	var result map[string]bool
	if err := c.do("POST", "/api/v1/memories/bulk_delete", body, &result); err != nil {
		return nil, err
	}
	return result, nil
}
