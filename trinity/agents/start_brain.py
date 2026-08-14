#!/usr/bin/env python3
"""
Trinity Agent Brain — One-Click Launcher
=========================================
Starts the autonomous memory brain, initializes all sub-modules,
and enters the main loop.  Runs as a foreground process by default;
use --daemon to background.  Use --bridge for A2A bridge mode.

Usage:
    python start_brain.py              # foreground
    python start_brain.py --daemon     # background daemon
    python start_brain.py --bridge     # bridge mode for Main Agent
    python start_brain.py --test       # run self-test only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Inject Trinity root into path (agents/ → trinity/ → project root)
_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the brain process."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy initialization logs from second_brain
    logging.getLogger("trinity.modules.second_brain").setLevel(logging.WARNING)


def load_config(config_path: str) -> dict:
    """Load agent config from YAML file with graceful fallback."""
    # config_path is currently unused but reserved for future YAML loading
    _ = config_path
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trinity Agent Brain — Autonomous Memory Manager"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run brain loop as background daemon thread"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run self-test only, then exit"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--cycle-interval", type=float, default=5.0,
        help="Main loop polling interval in seconds (default: 5.0)"
    )
    parser.add_argument(
        "--maintenance-interval", type=float, default=300.0,
        help="Maintenance interval in seconds (default: 300.0)"
    )
    parser.add_argument(
        "--bridge", action="store_true",
        help="Start in bridge mode: publish A2A context for Main Agent dispatch"
    )
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    logger = logging.getLogger("start_brain")

    # Determine config path
    trinity_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(trinity_dir, "agent_config.yaml")

    logger.info("Trinity Agent Brain — v1.1.0")
    logger.info("Project root: %s", _TRINITY_ROOT)
    logger.info("Config: %s", config_path)
    logger.info("Bridge mode: %s", "ENABLED" if args.bridge else "disabled")

    # Load config
    config = load_config(config_path)
    _ = config  # Reserved for future use

    # Import agent_brain
    try:
        from trinity.agents.agent_brain import AgentBrain, create_agent_brain
    except ImportError as e:
        logger.error("Failed to import agent_brain: %s", e)
        logger.error(
            "Ensure PYTHONPATH includes: %s", _TRINITY_ROOT
        )
        sys.exit(1)

    # Self-test mode
    if args.test:
        logger.info("Running self-test...")
        from trinity.agents.agent_brain import self_test
        ok = self_test()
        sys.exit(0 if ok else 1)

    # Create brain instance
    logger.info(
        "Initializing AgentBrain (cycle=%.1fs, maint=%.1fs)...",
        args.cycle_interval, args.maintenance_interval,
    )
    brain = create_agent_brain(
        cycle_interval=args.cycle_interval,
        maintenance_interval=args.maintenance_interval,
    )

    # Quick sanity: ingest a startup event
    brain.agent_protocol.on_agent_task_start(
        "main", "Trinity Agent Brain startup"
    )

    # Print status
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║     Trinity Agent Brain — v1.1.0                 ║")
    print("║     Autonomous Memory Manager                    ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Cycle Interval:      {args.cycle_interval:>6.1f}s                  ║")
    print(f"║  Maintenance:         {args.maintenance_interval:>6.1f}s                  ║")
    print(f"║  Mode:                {'daemon' if args.daemon else 'foreground':>11}             ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    try:
        if args.daemon:
            brain.run(daemon=True)
            logger.info("Brain daemon running. Press Ctrl+C to stop.")
            try:
                while brain.get_state().value != "stopped":
                    time.sleep(2)
            except KeyboardInterrupt:
                logger.info("Shutdown signal received")
            finally:
                brain.stop()
        else:
            # Foreground mode — run a few cycles for demo
            logger.info("Starting foreground loop (10 cycles)...")
            brain._running = True
            for i in range(10):
                if brain.get_state().value == "stopped":
                    break
                brain._cycle_count += 1

                # Simulate one cycle
                now = time.time()
                if now - brain._last_maintenance >= brain.maintenance_interval:
                    brain._last_maintenance = now
                    brain.scheduled_maintenance()
                if now - brain._last_consolidate >= brain.auto_consolidate_interval:
                    brain._last_consolidate = now
                    brain.auto_consolidate()
                if now - brain._last_conflict_check >= brain.conflict_check_interval:
                    brain._last_conflict_check = now
                    brain.auto_resolve_conflicts()

                time.sleep(brain.cycle_interval)

            brain._running = False
            brain.stop()
            logger.info("Foreground loop completed")
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        brain.stop()
    except Exception as exc:
        logger.error("Brain error: %s", exc, exc_info=True)
        brain.stop()
        sys.exit(1)

    # Final stats
    stats = brain.statistics()
    print("\nFinal statistics:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    logger.info("Trinity Agent Brain shut down gracefully")


if __name__ == "__main__":
    main()
