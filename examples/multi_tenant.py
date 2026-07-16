"""
Trinity — Multi-Tenant Example
================================
Demonstrates persona/session/tenant isolation.
"""

from trinity import Trinity

# Tenant-level isolation
mem_a = Trinity(tenant_id="company_a")
mem_b = Trinity(tenant_id="company_b")

mem_a.ingest("Company A's secret recipe", tags=["secret"])
mem_b.ingest("Company B's strategic plan", tags=["strategy"])

# Each tenant only sees their own data
for mem in [mem_a, mem_b]:
    results = mem.search("secret", top_k=5)
    print(f"Tenant {mem.tenant_id}: {len(results)} results")
    for r in results:
        print(f"  [{r['score']:.2f}] {r['content']}")
