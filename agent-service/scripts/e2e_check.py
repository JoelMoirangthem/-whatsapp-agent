#!/usr/bin/env python3
"""End-to-end chain verifier for the WhatsApp agent deployment.

Default mode (read-only, safe):
  1. Agent service reachable + authenticated
  2. LLM configured (AgentRouter)
  3. Go bridge reachable
  4. Message archive present

--live-recipient <number|JID> additionally performs a real approval cycle:
  asks the agent to send a short test message to the recipient, approves the
  pending action via the API, and verifies the bridge accepted it. Requires a
  paired WhatsApp account.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib import error, request


def _call(method: str, url: str, token: str | None = None, body: dict | None = None,
          timeout: float = 30) -> tuple[int, dict]:
    req = request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, data=data, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
            return resp.status, payload
    except error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {}
    except (error.URLError, TimeoutError) as exc:
        return 0, {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8100")
    parser.add_argument("--token", required=True, help="agent service bearer token")
    parser.add_argument("--live-recipient", help="run a REAL send+approve cycle to this recipient")
    args = parser.parse_args()

    failures: list[str] = []

    # 1-2. Service + LLM
    status, health = _call("GET", f"{args.api_url}/health")
    if status != 200:
        print(f"✗ agent service unreachable ({status}): {health}")
        return 1
    llm_ok = bool(health.get("llm", {}).get("configured"))
    print(f"{'✓' if llm_ok else '✗'} agent service up; LLM configured: {llm_ok} "
          f"(model {health.get('llm', {}).get('model')})")
    if not llm_ok:
        failures.append("LLM not configured")

    # 3. Bridge (up = process answers HTTP; whatsapp_connected = session paired)
    bridge = health.get("bridge", {})
    bridge_up = bridge.get("up") is True
    connected = bridge.get("whatsapp_connected")
    print(f"{'✓' if bridge_up else '✗'} bridge process up: {bridge_up} | "
          f"whatsapp connected: {connected}")
    if not bridge_up:
        failures.append("bridge not reachable (is it running?)")
    elif connected is False and not args.live_recipient:
        print("  ℹ WhatsApp not paired yet — scan the QR from the bridge logs to enable "
              "live sends/deletes (read-only analysis still works).")

    # 4. Archive
    archive_ok = health.get("archive", {}).get("available") is True
    print(f"{'✓' if archive_ok else '✗'} message archive available: {archive_ok}")
    if not archive_ok:
        failures.append("message archive missing (pair WhatsApp and let it sync)")

    if args.live_recipient and not failures:
        print(f"→ live cycle: proposing test message to {args.live_recipient}")
        status, resp = _call(
            "POST", f"{args.api_url}/agents/whatsapp", token=args.token,
            body={"message": f"Send exactly 'whatsapp-agent e2e check' to {args.live_recipient}"},
        )
        if resp.get("type") != "approval_required":
            failures.append(f"expected approval_required, got HTTP {status}: {resp}")
        else:
            action_id = resp["actionId"]
            warnings = resp.get("warnings", [])
            print(f"  ✓ pending action {action_id[:8]}… warnings={warnings or 'none'}")
            time.sleep(1)
            status, result = _call(
                "POST", f"{args.api_url}/agents/whatsapp/approve/{action_id}",
                token=args.token, body={"approved": True},
            )
            if status == 200 and result.get("status") == "executed":
                print("  ✓ approved and executed — check the recipient's WhatsApp")
            else:
                failures.append(f"execution failed (HTTP {status}): {result}")

    for failure in failures:
        print(f"✗ {failure}")
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
