#!/usr/bin/env python3
"""
Agentic Customer 360 — Proactive Intervention Desk (CLI)
Scenario 2: New Child Life Event (Priya Sharma)
No external packages required. Just Python 3.

Usage:
    python run_desk.py
    python run_desk.py --json
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
            "income_amount": 4500,
            "income_dip": False,
            "baby_purchases": [],
            "search_queries": [],
            "daycare_si": False,
            "dependents": 1,
            "support_tickets": [],
        }
        self.semantic = {
            "typical_salary": 4500,
            "baby_mcc": {"baby_products"},
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
        if any(k in text for k in ["child", "education", "savings", "baby", "daycare"]):
            board.publish("Usage", {
                "signal": "child_related_search",
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
    if "baby monitor" in raw or "confirming it's me" in raw:
        board.publish("Support", {
            "signal": "red_herring_clarified",
            "raw_text": p.get("raw_text"),
            "confidence": 0.95,
            "event_id": event["event_id"],
            "note": "Large electronics purchase confirmed as baby monitor — do not fraud-hold",
        })


def transaction_agent(event, board: StateBoard):
    src = event["source_system"]
    p = event["payload"]

    if src == "core_banking_ledger" and event["event_type"] == "deposit":
        if p.get("transaction_type") == "salary_credit":
            amt = float(p.get("amount", 0))
            board.working["income_amount"] = amt
            if amt < board.semantic["typical_salary"] * 0.75:
                board.working["income_dip"] = True
                board.publish("Transaction", {
                    "signal": "income_dip_maternity",
                    "amount": amt,
                    "typical": board.semantic["typical_salary"],
                    "confidence": 0.8,
                    "event_id": event["event_id"],
                })

    if src == "card_payments" and event["event_type"] == "purchase":
        mcc = p.get("mcc_category", "")
        amt = float(p.get("amount", 0))
        if mcc == "baby_products" or "baby" in p.get("merchant_name", "").lower():
            board.working["baby_purchases"].append({"merchant": p.get("merchant_name"), "amount": amt})
            board.publish("Transaction", {
                "signal": "baby_product_purchase",
                "merchant": p.get("merchant_name"),
                "amount": amt,
                "confidence": 0.75,
                "event_id": event["event_id"],
            })
        if mcc == "electronics" and amt >= 500:
            board.publish("Transaction", {
                "signal": "large_electronics",
                "merchant": p.get("merchant_name"),
                "amount": amt,
                "confidence": 0.6,
                "event_id": event["event_id"],
                "note": "Possible fraud signal — wait for support clarification",
            })

    if src == "core_banking_ledger" and event["event_type"] == "standing_instruction":
        ttype = p.get("transaction_type", "")
        if "daycare" in ttype.lower():
            board.working["daycare_si"] = True
            board.publish("Transaction", {
                "signal": "daycare_standing_instruction",
                "amount": float(p.get("amount", 0)),
                "confidence": 0.9,
                "event_id": event["event_id"],
            })


def kyc_agent(event, board: StateBoard):
    if event["source_system"] != "loan_kyc":
        return
    p = event["payload"]
    if event["event_type"] == "dependents_change" or p.get("event_subtype") == "dependents_change":
        old_v = p.get("old_value")
        new_v = p.get("new_value")
        board.working["dependents"] = new_v
        board.publish("KYC", {
            "signal": "dependents_increase",
            "old_value": old_v,
            "new_value": new_v,
            "confidence": 0.95,
            "event_id": event["event_id"],
        })


def life_event_and_synthesis(board: StateBoard):
    findings = board.swarm_findings
    signals = {f["signal"] for f in findings.values()}

    has_income_dip = "income_dip_maternity" in signals or board.working["income_dip"]
    has_baby_purchase = "baby_product_purchase" in signals or len(board.working["baby_purchases"]) > 0
    has_search = "child_related_search" in signals
    has_daycare = "daycare_standing_instruction" in signals or board.working["daycare_si"]
    has_dependents = "dependents_increase" in signals
    has_red_herring_cleared = "red_herring_clarified" in signals

    state, conf, action, subtype, hitl, notes, expl = (
        "no_significant_event", "low", "no_action", None, "auto_approved", "", []
    )

    if has_income_dip or has_baby_purchase:
        state = "new_child_life_event"
        conf = "low"
        notes = "Income dip and/or baby-store purchase. Signal still weak — do not promote yet."
        expl.append("Early signal: income dip / baby products")

    if has_search:
        state = "new_child_life_event"
        conf = "medium"
        notes = "Customer searched for child education savings. Evidence building."
        expl.append("Search: child education savings plan")

    if has_daycare or has_dependents:
        state = "new_child_life_event"
        conf = "high"
        action = "personalized_offer"
        subtype = "childcare_savings_or_insurance_plan"
        hitl = "escalated"
        notes = "Daycare standing instruction and/or KYC dependents change confirm new child. Route escalated personalized offer."
        if has_daycare:
            expl.append("Daycare standing instruction")
        if has_dependents:
            expl.append("KYC dependents 1 → 2")

    if has_red_herring_cleared and action == "compliance_fraud_hold":
        action = "no_action"
        notes += " | Red-herring suppressed (baby monitor confirmed)."

    board.inferred_state = state
    board.confidence = conf
    board.action = action
    board.action_subtype = subtype
    board.hitl_status = hitl
    board.notes = notes
    board.explanation = expl


CHECKPOINT_TIMES = [
    "2026-02-20T00:00:00Z",
    "2026-03-27T00:00:00Z",
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
    kyc_agent(event, board)
    life_event_and_synthesis(board)


def print_header():
    p = ENTITIES["profile"]
    print("=" * 64)
    print("  PROACTIVE INTERVENTION DESK")
    print("  Agentic Customer 360 · New Child Scenario")
    print("=" * 64)
    print(f"  Customer : {p['name']}  (age {p['age']}, {p['occupation']})")
    print(f"  ID       : {ENTITIES['customer_id']}")
    print(f"  Tier     : {p['customer_value_tier']}  ·  Tenure {p['tenure_months']} months")
    print(f"  Accounts : {', '.join(a['account_id'] for a in ENTITIES['accounts'])}")
    print("=" * 64)
    print()


def print_status(board: StateBoard):
    print(f"  State    : {board.inferred_state}")
    print(f"  Conf     : {board.confidence}")
    print(f"  Action   : {board.action}" + (f"  ({board.action_subtype})" if board.action_subtype else ""))
    print(f"  HITL     : {board.hitl_status}")
    if board.explanation:
        print(f"  Why      : {' | '.join(board.explanation)}")
    if board.swarm_findings:
        print(f"  Findings : {len(board.swarm_findings)} swarm signal(s)")
        for agent, f in board.swarm_findings.items():
            flag = " [RED HERRING CLEARED]" if "red_herring" in f.get("signal", "") else ""
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


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--json" in args:
        run_all(json_only=True)
    else:
        run_all()
