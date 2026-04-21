"""CLI entry: `python -m app.agent "<prompt>" [--deploy]`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from ..n8n.client import _connections_list_to_map
from .graph import run_cli


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _draft_to_n8n_payload(draft) -> dict:
    """Convert a WorkflowDraft to the plain n8n POST body dict for display."""
    raw = draft.model_dump(by_alias=True, exclude_none=True)
    raw["connections"] = _connections_list_to_map(draft.connections)
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.agent",
        description="Build an n8n workflow from a natural-language request.",
    )
    parser.add_argument("prompt", help="Natural-language workflow request.")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Also POST to n8n (requires N8N_API_KEY).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    t0 = time.monotonic()
    state = run_cli(args.prompt, deploy=args.deploy)
    elapsed = time.monotonic() - t0

    print("=" * 60)
    print("PLAN")
    print("=" * 60)
    for step in state.plan:
        print(f"- {step.step_id} [{step.intent}] {step.description}")
        print(f"    candidates: {step.candidate_node_types}")

    print()
    print("=" * 60)
    print("RETRIEVED NODE TYPES (discovery top-k)")
    print("=" * 60)
    for hit in state.discovery_hits:
        print(f"- {hit.get('type')}: {hit.get('display_name')}")

    print()
    print("=" * 60)
    print("VALIDATION")
    print("=" * 60)
    if state.validation:
        print(f"ok={state.validation.ok} errors={len(state.validation.errors)} "
              f"warnings={len(state.validation.warnings)} retry_count={state.retry_count}")
        for issue in state.validation.errors:
            print(f"  ERROR {issue.rule_id}: {issue.message} (path={issue.path})")
        for issue in state.validation.warnings[:5]:
            print(f"  WARN  {issue.rule_id}: {issue.message}")
    else:
        print("(no validation run)")

    print()
    print("=" * 60)
    print("WORKFLOW JSON")
    print("=" * 60)
    if state.draft is not None:
        payload = _draft_to_n8n_payload(state.draft)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("(no draft produced)")

    print()
    print("=" * 60)
    print("DEPLOY")
    print("=" * 60)
    if state.workflow_url:
        print(f"URL: {state.workflow_url}")
        print(f"ID:  {state.workflow_id}")
    else:
        print("(not deployed)")

    if state.error:
        print()
        print("=" * 60)
        print(f"ERROR: {state.error}")

    print()
    print(f"elapsed: {elapsed:.1f}s")
    return 0 if state.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
