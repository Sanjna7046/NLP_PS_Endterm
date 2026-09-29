#!/usr/bin/env python3
"""
Agentic Customer 360 — Proactive Intervention Desk (CLI version)
No external packages required. Just Python 3.

Usage:
    python run_desk.py              # process all events, show checkpoints
    python run_desk.py --step       # interactive step-by-step
    python run_desk.py --json       # only print final checkpoints JSON
"""

import json
import sys
from pathlib import Path

DATA = Path(__file__).parent
ENTITIES = json.loads((DATA / "entities.json").read_text())
LIVE = [json.loads(l) for l in (DATA / "live_stream.jsonl").read_text().strip().splitlines()]
GT = json.loads((DATA / "ground_truth.json").read_text())


class StateBoard:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        self.working = {
            "healthcare_spend_30d": 0.0,
            "pharmacy_spend_30d": 0.0,
            "income_type": "salary",
            "search_queries": [],
            "support_tickets": [],
        }
        self.semantic = {
            "typical_salary": 3800,
            "red_herring_counterparties": {"State University", "Oceanview Resort"},
        }
        self.swarm_findings = {}
        self.inferred_state = "no_significant_event"
        self.confidence = "low"
        self.action = "no_action"
        self.action_subtype = None
        self.hitl_status = "auto_approved"
        self.notes = ""
        self.checkpoints = []
        self.explanation = []

    def publish(self, agent: str, finding: dict):
        self.swarm_findings[agent] = finding


def usage_agent(event, board: StateBoard):
    if event["source_system"] != "web_app_events":
        return
    p = event["payload"]
    if event["event_type"] == "search_query":
        text = p.get("search_text", "").lower()
        board.working["search_queries"].append(text)
        if "hardship" in text or "medical" in text:
            board.publish("Usage", {
                "signal": "hardship_search",
                "text": text,
                "confidence": 0.85,
                "event_id": event["event_id"],
            })


def support_agent(event, board: StateBoard):
    if event["source_system"] != "support_logs":
        return
    p = event["payload"]
    board.working["support_tickets"].append(p)
    raw = p.get("raw_text", "").lower()
    if any(k in raw for k in ["hospital", "income dropped", "payment plan", "hardship"]):
        board.publish("Support", {
            "signal": "medical_support_ticket",
            "category": p.get("category"),
            "raw_text": p.get("raw_text"),
            "confidence": 0.95,
            "event_id": event["event_id"],
        })


def transaction_agent(event, board: StateBoard):
    src = event["source_system"]
    p = event["payload"]

    if src == "card_payments" and event["event_type"] == "purchase":
        mcc = p.get("mcc_category", "")
        amt = float(p.get("amount", 0))
        if mcc == "healthcare":
            board.working["healthcare_spend_30d"] += amt
            board.publish("Transaction", {
                "signal": "healthcare_spend",
                "merchant": p.get("merchant_name"),
                "amount": amt,
                "confidence": 0.7 if amt < 1000 else 0.9,
                "event_id": event["event_id"],
            })
        elif mcc == "pharmacy":
            board.working["pharmacy_spend_30d"] += amt
            board.publish("Transaction", {
                "signal": "pharmacy_spend",
                "merchant": p.get("merchant_name"),
                "amount": amt,
                "confidence": 0.65,
                "event_id": event["event_id"],
            })

    if src == "core_banking_ledger" and event["event_type"] == "deposit":
        if p.get("transaction_type") == "benefits_credit":
            board.working["income_type"] = "benefits"
            board.publish("Transaction", {
                "signal": "income_drop_to_benefits",
                "amount": float(p.get("amount", 0)),
                "confidence": 0.88,
                "event_id": event["event_id"],
            })

    if src == "ach_wire" and event["event_type"] == "outbound_transfer":
        cp = p.get("counterparty_name", "")
        if cp in board.semantic["red_herring_counterparties"]:
            board.publish("Transaction", {
                "signal": "red_herring_outbound",
                "amount": float(p.get("amount", 0)),
                "counterparty": cp,
                "confidence": 0.9,
                "event_id": event["event_id"],
            })

    if src == "card_payments" and event["event_type"] == "refund":
        if p.get("merchant_name") in board.semantic["red_herring_counterparties"]:
            board.publish("Transaction", {
                "signal": "red_herring_refund",
                "merchant": p.get("merchant_name"),
                "amount": p.get("amount"),
                "confidence": 0.9,
                "event_id": event["event_id"],
            })


def life_event_and_synthesis(board: StateBoard):
    findings = board.swarm_findings
    signals = {f["signal"] for f in findings.values()}

    has_er = any(f.get("signal") == "healthcare_spend" and f.get("amount", 0) >= 400 for f in findings.values())
    has_pharmacy = "pharmacy_spend" in signals
    has_income_drop = "income_drop_to_benefits" in signals
    has_large_hospital = any(f.get("signal") == "healthcare_spend" and f.get("amount", 0) >= 5000 for f in findings.values())
    has_hardship_search = "hardship_search" in signals
    has_support_ticket = "medical_support_ticket" in signals

    state, conf, action, subtype, hitl, notes, expl = (
        "no_significant_event", "low", "no_action", None, "auto_approved", "", []
    )

    if has_er or has_pharmacy:
        state, conf, notes = "medical_hardship", "low", "ER visit / pharmacy spend detected. Could still be minor."
        expl.append("Early healthcare signal (ER/pharmacy)")

    if has_income_drop:
        state, conf, notes = "medical_hardship", "medium", "Income replaced by reduced benefits + prior healthcare spend."
        expl.append("Salary → short-term disability benefits")

    if has_large_hospital:
        state, conf, notes = "medical_hardship", "medium", "Large hospital bill posted + income drop. Medical hardship likely."
        expl.append("Large hospital billing")

    if has_hardship_search:
        state, conf, notes = "medical_hardship", "high", "Customer explicitly searched for medical hardship plan."
        expl.append("Search: medical hardship plan")

    if has_support_ticket:
        state, conf = "medical_hardship", "high"
        action = "support_intervention"
        subtype = "medical_hardship_payment_plan"
        hitl = "escalated"
        notes = "Support ticket confirms hospitalization + income drop and requests payment plan. Escalated for human review."
        expl.append("Support ticket: payment plan request due to hospital + income drop")

    board.inferred_state = state
    board.confidence = conf
    board.action = action
    board.action_subtype = subtype
    board.hitl_status = hitl
    board.notes = notes
    board.explanation = expl


CHECKPOINT_TIMES = [
    "2026-02-15T00:00:00Z",
    "2026-03-12T00:00:00Z",
    "2026-03-26T00:00:00Z",
]


def maybe_emit_checkpoint(board: StateBoard, current_time: str):
    for t in CHECKPOINT_TIMES:
        if current_time >= t and not any(c["as_of_time"] == t for c in board.checkpoints):
            board.checkpoints.append({
                "as_of_time": t,
                "inferred_state": board.inferred_state,
                "confidence_band": board.confidence,
                "action": board.action,
                "action_subtype": board.action_subtype,
                "hitl_status": board.hitl_status,
                "notes": board.notes,
            })


def process_event(event, board: StateBoard):
    usage_agent(event, board)
    support_agent(event, board)
    transaction_agent(event, board)
    life_event_and_synthesis(board)


def print_header():
    p = ENTITIES["profile"]
    print("=" * 64)
    print("  PROACTIVE INTERVENTION DESK")
    print("  Agentic Customer 360 · Medical Hardship Scenario")
    print("=" * 64)
    print(f"  Customer : {p['name']}  (age {p['age']}, {p['occupation']})")
    print(f"  ID       : {ENTITIES['customer_id']}")
    print(f"  Tier     : {p['customer_value_tier']}  ·  Tenure {p['tenure_months']} months")
    print(f"  Accounts : {', '.join(a['account_id'] for a in ENTITIES['accounts'])}")
    print("=" * 64)
    print()


def print_status(board: StateBoard, event=None):
    if event:
        print(f"  Event    : {event['event_id']}  @ {event['event_time'][:16]}")
        print(f"  Source   : {event['source_system']} / {event['event_type']}")
    print(f"  State    : {board.inferred_state}")
    print(f"  Conf     : {board.confidence}")
    print(f"  Action   : {board.action}" + (f"  ({board.action_subtype})" if board.action_subtype else ""))
    print(f"  HITL     : {board.hitl_status}")
    if board.explanation:
        print(f"  Why      : {' | '.join(board.explanation)}")
    if board.swarm_findings:
        print(f"  Findings : {len(board.swarm_findings)} swarm signal(s)")
        for agent, f in board.swarm_findings.items():
            flag = " [RED HERRING]" if "red_herring" in f.get("signal", "") else ""
            print(f"             · {agent}: {f.get('signal')}{flag}")
    print()


def run_all(json_only=False):
    board = StateBoard(ENTITIES["customer_id"])
    for ev in LIVE:
        process_event(ev, board)
        maybe_emit_checkpoint(board, ev["event_time"])

    if json_only:
        print(json.dumps(board.checkpoints, indent=2))
        return

    print_header()
    print("  Processed all", len(LIVE), "live events.\n")
    print("  ── Final Decision ──")
    print_status(board)
    print("  ── Emitted Checkpoints (required schema) ──")
    print(json.dumps(board.checkpoints, indent=2))
    print()

    print("  ── Ground-truth match ──")
    for i, (ours, exp) in enumerate(zip(board.checkpoints, GT["checkpoints"])):
        s = "OK" if ours["inferred_state"] == exp["expected_inferred_state"] else "MISS"
        c = "OK" if ours["confidence_band"] == exp["expected_confidence_band"] else "MISS"
        a = "OK" if ours["action"] == exp["expected_action"] else "MISS"
        print(f"  CP{i+1} ({ours['as_of_time'][:10]}): state={s}  conf={c}  action={a}")
    print()

    out = DATA / "checkpoints.json"
    out.write_text(json.dumps(board.checkpoints, indent=2))
    print(f"  Saved → {out}")
    print()


def run_step():
    print_header()
    board = StateBoard(ENTITIES["customer_id"])
    print("  Press ENTER to process next event  |  q + ENTER to quit and show final result\n")

    for i, ev in enumerate(LIVE):
        process_event(ev, board)
        maybe_emit_checkpoint(board, ev["event_time"])

        interesting = (
            ev["source_system"] in ("support_logs",)
            or ev["payload"].get("mcc_category") in ("healthcare", "pharmacy")
            or ev["payload"].get("transaction_type") == "benefits_credit"
            or ev["event_type"] in ("search_query", "outbound_transfer", "refund")
            or "hospital" in str(ev["payload"]).lower()
            or "hardship" in str(ev["payload"]).lower()
        )
        if interesting or (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(LIVE)}]")
            print_status(board, ev)

        if interesting:
            cmd = input("  > ").strip().lower()
            if cmd == "q":
                break

    print("\n  ── Final Checkpoints ──")
    print(json.dumps(board.checkpoints, indent=2))
    out = DATA / "checkpoints.json"
    out.write_text(json.dumps(board.checkpoints, indent=2))
    print(f"\n  Saved → {out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--step" in args:
        run_step()
    elif "--json" in args:
        run_all(json_only=True)
    else:
        run_all()
