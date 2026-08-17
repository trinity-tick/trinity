package trinity

import (
	"encoding/json"
	"fmt"
	"testing"
)

// ── Self-test ──────────────────────────────────────────────────────────

// SelfTestResult mirrors the Python self_test output format.
type SelfTestResult struct {
	Module  string           `json:"module"`
	Passed  int              `json:"passed"`
	Failed  int              `json:"failed"`
	Total   int              `json:"total"`
	Details []SelfTestDetail `json:"details"`
}

type SelfTestDetail struct {
	Test   string `json:"test"`
	Status string `json:"status"`
	Reason string `json:"reason,omitempty"`
}

func SelfTest() SelfTestResult {
	r := SelfTestResult{Module: "P2-1b_go_sdk"}

	pass := func(name string) {
		r.Passed++
		r.Total++
		r.Details = append(r.Details, SelfTestDetail{Test: name, Status: "PASS"})
	}
	fail := func(name, reason string) {
		r.Failed++
		r.Total++
		r.Details = append(r.Details, SelfTestDetail{Test: name, Status: "FAIL", Reason: reason})
	}

	// Test 1: Client instantiation
	c := NewClient(ClientConfig{Endpoint: "http://localhost:8100"})
	if c != nil {
		pass("Client instantiation")
	} else {
		fail("Client instantiation", "nil client")
	}

	// Test 2: Config defaults
	if c.timeout > 0 && c.maxRetries == 3 {
		pass("Config defaults")
	} else {
		fail("Config defaults", fmt.Sprintf("timeout=%v retries=%d", c.timeout, c.maxRetries))
	}

	// Test 3: Endpoint normalization
	c2 := NewClient(ClientConfig{Endpoint: "http://localhost:8100/"})
	if c2.endpoint == "http://localhost:8100" {
		pass("Endpoint normalization")
	} else {
		fail("Endpoint normalization", fmt.Sprintf("got=%s", c2.endpoint))
	}

	// Test 4: API key handling
	c3 := NewClient(ClientConfig{Endpoint: "http://localhost:8100", APIKey: "sk-test"})
	if c3.apiKey == "sk-test" {
		pass("API key handling")
	} else {
		fail("API key handling", "wrong key")
	}

	// Test 5: Health returns *HealthStatus
	_ = c.Health // method exists
	pass("Health method signature")

	// Test 6: Memory CRUD method signatures
	_ = c.CreateMemory
	_ = c.GetMemory
	_ = c.UpdateMemory
	_ = c.DeleteMemory
	_ = c.ListMemories
	pass("Memory CRUD signatures")

	// Test 7: Search method signatures
	_ = c.SearchMemories
	_ = c.SemanticSearch
	_ = c.KeywordSearch
	_ = c.HybridSearch
	pass("Search method signatures")

	// Test 8: JSON serialization round-trip
	rec := MemoryRecord{
		MemoryID:  "mem_test",
		Content:   "test content",
		Status:    "active",
		Tags:      []string{"go", "sdk"},
		Confidence: 0.95,
	}
	data, err := json.Marshal(rec)
	if err != nil {
		fail("JSON marshal", err.Error())
	} else {
		var rec2 MemoryRecord
		if err := json.Unmarshal(data, &rec2); err != nil {
			fail("JSON unmarshal", err.Error())
		} else if rec2.MemoryID != "mem_test" || rec2.Confidence != 0.95 {
			fail("JSON round-trip", "data mismatch")
		} else {
			pass("JSON round-trip")
		}
	}

	return r
}

// TestSelfTest runs the self-test function.
func TestSelfTest(t *testing.T) {
	r := SelfTest()
	if r.Failed > 0 {
		for _, d := range r.Details {
			if d.Status == "FAIL" {
				t.Errorf("%s: %s", d.Test, d.Reason)
			}
		}
	}
	data, _ := json.MarshalIndent(r, "", "  ")
	t.Logf("\n%s", string(data))
}
