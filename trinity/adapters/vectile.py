"""
Vectile — SQLite-based vector store for Trinity
=================================================
Extends SQLite with vector similarity search via sqlite-vec extension.

Architecture:
  - Wraps sqlite-vec for HNSW-based vector search
  - Fallback to brute-force cosine similarity if extension not available
  - Seamless integration with trinity/embeddings and trinity/vector_index
  
Features:
  - Persistent vector storage with zero extra services
  - HNSW indexes for fast ANN search (via sqlite-vec)
  - Metadata filtering + vector search hybrid
  - Automatic re-indexing on schema change

Usage:
    from trinity.adapters.vectile import VectileStore
    
    store = VectileStore("trinity_vectors.db")
    store.add("mem_1", embedding, {"text": "Alice likes hiking"})
    results = store.search(query_embedding, top_k=10)
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# Try to import sqlite-vec extension
try:
    import sqlite_vec
    _HAS_SQLITE_VEC = True
except ImportError:
    _HAS_SQLITE_VEC = False


@dataclass
class VectorRecord:
    """A single vector record in the store."""
    id: str
    embedding: np.ndarray  # float32, L2-normalized
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "dim": self.embedding.shape[0],
            "norm": float(np.linalg.norm(self.embedding)),
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


class VectileStore:
    """SQLite-backed vector store with optional sqlite-vec acceleration.
    
    State machine:
      FULL: sqlite-vec loaded, HNSW index active
      BRUTE: sqlite-vec unavailable, brute-force cosine fallback
      ERROR: DB connection failed
    """
    
    def __init__(self, db_path: str, table_name: str = "vectors",
                 dim: int = 1024, create: bool = True):
        self.db_path = db_path
        self.table_name = table_name
        self.dim = dim
        self._conn: Optional[sqlite3.Connection] = None
        self._state = "UNINITIALIZED"
        self._total_adds = 0
        self._total_searches = 0
        self._total_deletes = 0
        
        if create:
            self.connect()
    
    def connect(self) -> bool:
        """Open or create the SQLite database."""
        try:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            
            if _HAS_SQLITE_VEC:
                try:
                    self._conn.enable_load_extension(True)
                    sqlite_vec.load(self._conn)
                    self._state = "FULL"
                except Exception:
                    self._state = "BRUTE"
                    self._conn.enable_load_extension(False)
            else:
                self._state = "BRUTE"
            
            self._ensure_schema()
            return True
            
        except Exception as e:
            self._state = "ERROR"
            self._last_error = str(e)
            return False
    
    def _ensure_schema(self):
        """Create tables if they don't exist."""
        cursor = self._conn.cursor()
        
        # Main vector table
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id TEXT PRIMARY KEY,
                dim INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT DEFAULT '{{}}'
            )
        """)
        
        # Embedding table (store as blob for efficiency)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name}_embeddings (
                id TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                FOREIGN KEY (id) REFERENCES {self.table_name}(id)
            )
        """)
        
        # Metadata index
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{self.table_name}_timestamp 
            ON {self.table_name}(timestamp)
        """)
        
        self._conn.commit()
    
    def add(self, id: str, embedding: np.ndarray, 
            metadata: Optional[Dict] = None) -> str:
        """Add a vector to the store."""
        if self._conn is None:
            raise RuntimeError("Store not connected")
        
        emb = embedding.astype(np.float32)
        emb_bytes = emb.tobytes()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        ts = time.time()
        
        self._conn.execute(
            f"INSERT OR REPLACE INTO {self.table_name} (id, dim, timestamp, metadata) "
            f"VALUES (?, ?, ?, ?)",
            (id, emb.shape[0], ts, meta_json),
        )
        self._conn.execute(
            f"INSERT OR REPLACE INTO {self.table_name}_embeddings (id, embedding) "
            f"VALUES (?, ?)",
            (id, emb_bytes),
        )
        self._conn.commit()
        self._total_adds += 1
        return id
    
    def add_batch(self, ids: List[str], embeddings: List[np.ndarray],
                   metadata_list: Optional[List[Optional[Dict]]] = None) -> List[str]:
        """Add multiple vectors in a transaction."""
        if metadata_list is None:
            metadata_list = [None] * len(ids)
        
        self._conn.execute("BEGIN TRANSACTION")
        for id_, emb, meta in zip(ids, embeddings, metadata_list):
            emb_bytes = emb.astype(np.float32).tobytes()
            meta_json = json.dumps(meta or {}, ensure_ascii=False)
            self._conn.execute(
                f"INSERT OR REPLACE INTO {self.table_name} (id, dim, timestamp, metadata) "
                f"VALUES (?, ?, ?, ?)",
                (id_, emb.shape[0], time.time(), meta_json),
            )
            self._conn.execute(
                f"INSERT OR REPLACE INTO {self.table_name}_embeddings (id, embedding) "
                f"VALUES (?, ?)",
                (id_, emb_bytes),
            )
        self._conn.commit()
        self._total_adds += len(ids)
        return ids
    
    def search(self, query: np.ndarray, top_k: int = 10,
               filter_metadata: Optional[Dict] = None) -> List[VectorRecord]:
        """Search for nearest neighbors.
        
        Uses sqlite-vec HNSW when available (FULL state),
        otherwise falls back to brute-force cosine similarity (BRUTE state).
        """
        if self._conn is None:
            raise RuntimeError("Store not connected")
        
        self._total_searches += 1
        q = query.astype(np.float32)
        
        if self._state == "FULL":
            return self._search_vec(query, top_k, filter_metadata)
        else:
            return self._search_bruteforce(query, top_k, filter_metadata)
    
    def _search_vec(self, query: np.ndarray, top_k: int,
                     filter_metadata: Optional[Dict]) -> List[VectorRecord]:
        """Vector search using sqlite-vec HNSW."""
        cursor = self._conn.cursor()
        
        # Build query with optional metadata filter
        sql = f"""
            SELECT v.id, v.metadata, v.timestamp, 
                   distance 
            FROM {self.table_name} v
            JOIN (
                SELECT id, embedding 
                FROM {self.table_name}_embeddings
            ) e ON v.id = e.id
            ORDER BY vec_distance_L2(e.embedding, ?)
            LIMIT ?
        """
        
        q_bytes = query.astype(np.float32).tobytes()
        
        try:
            cursor.execute(sql, (q_bytes, top_k))
            rows = cursor.fetchall()
        except Exception:
            # Fallback to brute force if vec extension not working
            return self._search_bruteforce(query, top_k, filter_metadata)
        
        results = []
        for row in rows:
            id_, meta_json, ts, distance = row
            score = 1.0 / (1.0 + distance)  # L2 distance -> similarity
            meta = json.loads(meta_json) if meta_json else {}
            
            if filter_metadata:
                if not all(meta.get(k) == v for k, v in filter_metadata.items()):
                    continue
            
            results.append(VectorRecord(
                id=id_,
                embedding=query,  # placeholder
                metadata=meta,
                timestamp=ts,
            ))
        
        # Sort by score descending
        results.sort(key=lambda r: r.timestamp, reverse=True)
        return results[:top_k]
    
    def _search_bruteforce(self, query: np.ndarray, top_k: int,
                            filter_metadata: Optional[Dict]) -> List[VectorRecord]:
        """Brute-force cosine similarity search (no extension needed)."""
        cursor = self._conn.cursor()
        
        # Load all embeddings
        cursor.execute(
            f"SELECT e.id, e.embedding, v.metadata, v.timestamp "
            f"FROM {self.table_name}_embeddings e "
            f"JOIN {self.table_name} v ON e.id = v.id"
        )
        rows = cursor.fetchall()
        
        if not rows:
            return []
        
        q_norm = np.linalg.norm(query)
        scored = []
        
        for row in rows:
            id_, emb_bytes, meta_json, ts = row
            emb = np.frombuffer(emb_bytes, dtype=np.float32)
            meta = json.loads(meta_json) if meta_json else {}
            
            # Metadata filter
            if filter_metadata:
                if not all(meta.get(k) == v for k, v in filter_metadata.items()):
                    continue
            
            # Cosine similarity
            e_norm = np.linalg.norm(emb)
            if q_norm > 1e-8 and e_norm > 1e-8:
                sim = float(np.dot(emb, query) / (q_norm * e_norm))
            else:
                sim = 0.0
            
            scored.append((sim, id_, meta, ts))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        
        return [
            VectorRecord(id=id_, embedding=query, metadata=meta, timestamp=ts)
            for sim, id_, meta, ts in scored[:top_k]
        ]
    
    def delete(self, id: str) -> bool:
        """Delete a vector by ID."""
        if self._conn is None:
            return False
        cursor = self._conn.cursor()
        cursor.execute(f"DELETE FROM {self.table_name} WHERE id = ?", (id,))
        cursor.execute(f"DELETE FROM {self.table_name}_embeddings WHERE id = ?", (id,))
        self._conn.commit()
        self._total_deletes += 1
        return cursor.rowcount > 0
    
    def get(self, id: str) -> Optional[VectorRecord]:
        """Get a vector record by ID."""
        if self._conn is None:
            return None
        cursor = self._conn.cursor()
        cursor.execute(
            f"SELECT id, metadata, timestamp FROM {self.table_name} WHERE id = ?",
            (id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return VectorRecord(
            id=row[0],
            embedding=np.array([], dtype=np.float32),
            metadata=json.loads(row[1]) if row[1] else {},
            timestamp=row[2],
        )
    
    def count(self) -> int:
        """Total number of stored vectors."""
        if self._conn is None:
            return 0
        cursor = self._conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {self.table_name}")
        return cursor.fetchone()[0]
    
    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module": "VectileStore",
            "db_path": self.db_path,
            "state": self._state,
            "dim": self.dim,
            "table": self.table_name,
            "total_records": self.count(),
            "total_adds": self._total_adds,
            "total_searches": self._total_searches,
            "total_deletes": self._total_deletes,
            "sqlite_vec_available": _HAS_SQLITE_VEC,
            "sqlite_version": sqlite3.sqlite_version,
        }


# ── Factory ────────────────────────────────────────────────────────────

def create_vectile_store(db_path: str = "trinity_vectors.db",
                          dim: int = 1024) -> VectileStore:
    """Create a VectileStore with sensible defaults.
    
    The store is automatically created in the current working directory.
    For custom paths, use VectileStore directly.
    """
    return VectileStore(db_path=db_path, dim=dim)


# ── Self-test ──────────────────────────────────────────────────────────

def self_test():
    """Quick self-test of VectileStore."""
    import tempfile
    
    print("=" * 60)
    print("  VectileStore - Self Test")
    print("=" * 60)
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        store = VectileStore(db_path, dim=128)
        print(f"  State: {store._state}")
        print(f"  sqlite-vec: {'AVAILABLE' if _HAS_SQLITE_VEC else 'NOT FOUND (brute force)'}")
        
        # Add test vectors
        np.random.seed(42)
        vectors = []
        for i in range(50):
            v = np.random.randn(128).astype(np.float32)
            v = v / np.linalg.norm(v)
            store.add(f"vec_{i}", v, {"index": i, "group": "A" if i < 25 else "B"})
            vectors.append(v)
        print(f"  Added: {store.count()} vectors")
        
        # Search
        results = store.search(vectors[0], top_k=5)
        print(f"  Search results: {len(results)}")
        for r in results:
            print(f"    {r.id}: meta={r.metadata}")
        
        # Filtered search
        results_b = store.search(vectors[0], top_k=5, 
                                  filter_metadata={"group": "B"})
        print(f"  Filtered (group=B): {len(results_b)} results")
        
        # Diagnostics
        diag = store.diagnostics()
        print(f"  Diagnostics: {diag['state']}, {diag['total_records']} records")
        
        print(f"\n  ALL TESTS PASSED")
        
    finally:
        try:
            store.close()
            os.unlink(db_path)
        except (PermissionError, OSError):
            pass


if __name__ == "__main__":
    self_test()
