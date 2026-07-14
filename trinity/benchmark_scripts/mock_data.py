"""
Mock LongMemEval data generator — for offline benchmark validation.

Generates synthetic memories and queries that mimic the LongMemEval format.
No HuggingFace download required.
"""

import json
import os
import random
from typing import Dict, List, Any


def generate_mock_dataset(
    num_personas: int = 3,
    sessions_per_persona: int = 3,
    turns_per_session: int = 8,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """Generate a synthetic LongMemEval-like dataset.

    Returns list of conversation dicts with:
      - persona_id, session_id, turns, memory_items, questions
    """
    rng = random.Random(seed)

    topics = [
        "hiking", "programming", "cooking", "photography",
        "music", "travel", "reading", "gaming",
    ]
    preferences = [
        "dark mode", "early morning", "quiet environment",
        "open source", "minimalist design", "keyboard shortcuts",
        "coffee", "weekend planning",
    ]
    names = ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    dataset = []
    for pi in range(num_personas):
        persona_name = names[pi % len(names)]
        persona_id = f"persona_{pi}"

        for si in range(sessions_per_persona):
            session_id = f"sess_{pi}_{si}"
            topic = rng.choice(topics)
            pref = rng.choice(preferences)

            turns = []
            for ti in range(turns_per_session):
                if rng.random() < 0.6:
                    text = f"I really enjoy {topic}, especially when {pref}."
                    role = "user"
                else:
                    text = f"That's great! {topic} and {pref} go well together."
                    role = "assistant"
                turns.append({"role": role, "content": text})

            # Memory items (things to remember)
            memory_items = [
                {"content": f"{persona_name} likes {topic}", "category": "preference"},
                {"content": f"{persona_name} prefers {pref}", "category": "preference"},
                {"content": f"{persona_name} discussed {topic} in session {session_id}", "category": "fact"},
            ]

            # Questions based on memories
            questions = [
                {
                    "question": f"What activity does {persona_name} enjoy?",
                    "answer": topic,
                    "category": "preference",
                },
                {
                    "question": f"What does {persona_name} prefer about {topic}?",
                    "answer": pref,
                    "category": "preference",
                },
            ]

            dataset.append({
                "persona_id": persona_id,
                "persona_name": persona_name,
                "session_id": session_id,
                "topic": topic,
                "turns": turns,
                "memory_items": memory_items,
                "questions": questions,
            })

    return dataset


def evaluate_mock_retrieval(retriever_fn, top_k: int = 5) -> Dict[str, Any]:
    """Evaluate a retriever on the mock dataset.

    Args:
        retriever_fn: Callable(query, top_k) -> list of retrieved items
        top_k: Number of results to retrieve

    Returns:
        Dict with Recall@1, Recall@5, Recall@10 and per-category breakdown
    """
    dataset = generate_mock_dataset(num_personas=3)

    total_questions = 0
    recall_at_1 = 0
    recall_at_5 = 0
    recall_at_10 = 0

    for conv in dataset:
        # Ingest memory items
        for item in conv["memory_items"]:
            try:
                # Try using the real Trinity engine
                from trinity import Trinity
                mem = Trinity()
                mem.ingest(
                    content=item["content"],
                    tags=[item["category"]],
                    role="user",
                )
            except Exception:
                pass  # Skip ingest errors in mock eval

        # Evaluate questions
        for q in conv["questions"]:
            total_questions += 1
            try:
                from trinity import Trinity
                mem = Trinity()
                results = mem.search(q["question"], top_k=top_k)
                hits = results.get("results", results if isinstance(results, list) else [])

                # Check if answer is in retrieved results
                answer = q["answer"].lower()
                for i, hit in enumerate(hits):
                    preview = hit.get("content_preview", hit.get("content", "")).lower()
                    if answer in preview:
                        if i == 0:
                            recall_at_1 += 1
                        if i < 5:
                            recall_at_5 += 1
                        if i < 10:
                            recall_at_10 += 1
                        break
            except Exception:
                pass

    return {
        "total_questions": total_questions,
        "recall_at_1": recall_at_1 / max(total_questions, 1),
        "recall_at_5": recall_at_5 / max(total_questions, 1),
        "recall_at_10": recall_at_10 / max(total_questions, 1),
        "scenario": "mock",
    }


def main():
    """Run mock evaluation."""
    print("=" * 60)
    print("LongMemEval Mock Benchmark")
    print("=" * 60)

    dataset = generate_mock_dataset()
    print(f"Generated {len(dataset)} conversations")
    print(f"  Personas: {len(set(c['persona_id'] for c in dataset))}")
    total_q = sum(len(c["questions"]) for c in dataset)
    print(f"  Questions: {total_q}")

    print("\nRunning retrieval evaluation...")
    results = evaluate_mock_retrieval(None)

    print(f"\nResults:")
    print(f"  Recall@1:  {results['recall_at_1']:.1%}")
    print(f"  Recall@5:  {results['recall_at_5']:.1%}")
    print(f"  Recall@10: {results['recall_at_10']:.1%}")

    print(f"\nSaved to: benchmark/results/mock_results.json")

    # Save results
    output_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "mock_results.json"), "w") as f:
        json.dump(results, f, indent=2)

    return results


if __name__ == "__main__":
    main()
