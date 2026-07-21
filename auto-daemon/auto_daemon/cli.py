"""
auto-daemon CLI — check inputs and manage guardian chain.

Usage:
    auto-daemon check "user input text"
    auto-daemon check --file input.txt
    auto-daemon diagnostics
    auto-daemon tiers
    auto-daemon tier L1 --description
"""

import argparse
import json
import sys
from auto_daemon.engine import GuardianChain, TIER_REGISTRY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="auto-daemon — 50-tier Guardian Chain for LLM Safety",
    )
    parser.add_argument("--version", action="version", version="auto-daemon 1.0.0")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # check
    p_check = sub.add_parser("check", help="Check content through guardian chain")
    p_check.add_argument("content", nargs="?", help="Content to check")
    p_check.add_argument("--file", "-f", help="Read content from file")
    p_check.add_argument("--role", default="user", help="Role: user/assistant/system")
    p_check.add_argument("--max-tiers", type=int, default=None, help="Max tiers to check")
    p_check.add_argument("--json", action="store_true", help="Output as JSON")

    # diagnostics
    sub.add_parser("diagnostics", help="Print guardian chain diagnostics")

    # tiers
    p_tiers = sub.add_parser("tiers", help="List all available tiers")
    p_tiers.add_argument("--group", help="Filter by group (input/behavior/execution/audit/reasoning)")
    p_tiers.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    guard = GuardianChain()

    if args.command == "check":
        content = args.content
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        if not content:
            print("Error: provide content or --file")
            sys.exit(1)

        context = {"role": args.role}
        result = guard.check(content, context, max_tiers=args.max_tiers)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(result.summary())
            if result.blocks:
                print("\nBlocks:")
                for b in result.blocks:
                    print(f"  ❌ {b}")
            if result.warnings:
                print("\nWarnings:")
                for w in result.warnings:
                    print(f"  ⚠️  {w}")

    elif args.command == "diagnostics":
        diag = guard.diagnostics()
        print(json.dumps(diag, indent=2, ensure_ascii=False))

    elif args.command == "tiers":
        tiers = list(TIER_REGISTRY.values())
        if args.group:
            tiers = [t for t in tiers if t["group"] == args.group]
        
        if args.json:
            print(json.dumps(tiers, indent=2, ensure_ascii=False))
        else:
            print(f"Available tiers ({len(tiers)} total):")
            print("=" * 60)
            for t in tiers:
                print(f"  {t['tier_id'] if 'tier_id' in t else '--'}: {t['name']}")
                print(f"      Group: {t['group']} | {t['description']}")
            print("=" * 60)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
