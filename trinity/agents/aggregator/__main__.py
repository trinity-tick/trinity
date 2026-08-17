"""Entry point: python -m trinity.agents.aggregator runs the MemoryAggregator self-test."""
from . import self_test

if __name__ == "__main__":
    ok = self_test()
    raise SystemExit(0 if ok else 1)
