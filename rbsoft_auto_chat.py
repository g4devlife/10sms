import os
import json
import time
import uuid
import random
import tempfile
import threading
from typing import Dict, Any, List, Tuple, Optional, Set

import requests

# =========================
# CONFIG
# =========================

BASE_URL = "https://smsgateway.rbsoft.org"
TOKEN = os.getenv("RBSOFT_TOKEN", "")  # Bearer token from RBSoft dashboard

STATE_FILE = os.getenv("STATE_FILE", "rbsoft_state.json")

MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "4"))
SIM_REFRESH_INTERVAL_S = int(os.getenv("SIM_REFRESH_INTERVAL_S", "30"))
REPLY_DELAY_MIN_S = int(os.getenv("REPLY_DELAY_MIN_S", "2"))
REPLY_DELAY_MAX_S = int(os.getenv("REPLY_DELAY_MAX_S", "7"))

GLOBAL_SEND_PER_MIN = int(os.getenv("GLOBAL_SEND_PER_MIN", "120"))
PER_SIM_SEND_PER_MIN = int(os.getenv("PER_SIM_SEND_PER_MIN", "30"))

TEMPLATES = [
    "Hello 👋",
    "Comment ça va aujourd’hui ?",
    "Tu fais quoi de beau en ce moment ?",
    "La journée s’est bien passée ?",
    "Tu as bien mangé ? 😄",
    "Tu as des nouvelles ?",
    "Tu bosses sur quoi ces jours-ci ?",
    "Ça fait plaisir d’avoir de tes nouvelles.",
    "On se capte bientôt !",
    "Bon je te laisse — prends soin de toi 🙏"
]

# =========================
# HTTP helpers
# =========================

def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def api_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def api_post(path: str, payload: dict) -> dict:
    url = f"{BASE_URL}{path}"
    r = requests.post(url, headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}

# =========================
# State (JSON) atomic save
# =========================

_lock = threading.Lock()

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "conversations": {},          # conv_id -> {...}
            "waiting_number": None,       # if odd SIM count, keep one waiting here
            "known_sims": {},             # number -> sim_id (last refresh)
            "dedupe_msg_ids": {},         # message_id -> ts
            "rate": {"global": [], "per_sim": {}},
            "meta": {"last_sim_refresh": 0}
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def atomic_save(state: Dict[str, Any]) -> None:
    fd, tmp_path = tempfile.mkstemp(prefix="rbsoft_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(state, tmp, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# =========================
# Rate limiting
# =========================

def _prune(ts_list: List[float], window_s: int = 60) -> List[float]:
    now = time.time()
    return [t for t in ts_list if (now - t) <= window_s]

def can_send(state: Dict[str, Any], sim_id: int) -> bool:
    now = time.time()
    rate = state.setdefault("rate", {"global": [], "per_sim": {}})
    rate["global"] = _prune(rate.get("global", []))
    per = rate.setdefault("per_sim", {})
    key = str(sim_id)
    per[key] = _prune(per.get(key, []))

    if len(rate["global"]) >= GLOBAL_SEND_PER_MIN:
        return False
    if len(per[key]) >= PER_SIM_SEND_PER_MIN:
        return False

    rate["global"].append(now)
    per[key].append(now)
    return True

# =========================
# SIM discovery
# =========================

def fetch_sims() -> Dict[str, int]:
    """
    GET /api/v1/devices
    Returns mapping: SIM phone number -> sim_id
    """
    data = api_get("/api/v1/devices")
    sims_map: Dict[str, int] = {}
    for dev in data.get("data", []):
        for sim in dev.get("sims", []):
            number = sim.get("number")
            sim_id = sim.get("id")
            if number and sim_id:
                sims_map[number] = int(sim_id)
    return sims_map

# =========================
# Conversations
# =========================

def build_text(conv_id: str, turn: int) -> str:
    base = TEMPLATES[min(max(turn - 1, 0), len(TEMPLATES) - 1)]
    # Tag anti-boucle + debug
    return f"[TEST conv={conv_id} turn={turn}] {base}"

def send_sms(sim_id: int, to_number: str, message: str) -> None:
    """
    POST /api/v1/messages/send
    We create a single-message campaign to one number using one SIM.
    """
    payload = {
        "sims": [sim_id],
        "mobile_numbers": [to_number],
        "type": "SMS",
        "message": message,
        "delivery_report": False,
        "prioritize": True,
        "name": f"AutoChat-{uuid.uuid4().hex[:6]}",
    }
    api_post("/api/v1/messages/send", payload)

def conv_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))

def build_existing_pairs_index(state: Dict[str, Any]) -> Set[str]:
    idx = set()
    for c in state.get("conversations", {}).values():
        idx.add(conv_key(c["a_number"], c["b_number"]))
    return idx

def start_one_conversation(state: Dict[str, Any], a_num: str, b_num: str, sims_map: Dict[str, int]) -> Optional[str]:
    a_sim = sims_map.get(a_num)
    b_sim = sims_map.get(b_num)
    if not a_sim or not b_sim:
        return None

    cid = uuid.uuid4().hex[:10]
    state["conversations"][cid] = {
        "id": cid,
        "a_number": a_num,
        "b_number": b_num,
        "a_sim": a_sim,
        "b_sim": b_sim,
        "turn": 0,
        "max_turns": MAX_TURNS,
        "status": "active",
        "last_sender": None,
        "created_at": time.time(),
    }

    # initial message A -> B (turn 1)
    if can_send(state, a_sim):
        send_sms(a_sim, b_num, build_text(cid, 1))
        state["conversations"][cid]["turn"] = 1
        state["conversations"][cid]["last_sender"] = a_num

    return cid

def adaptive_pairing(state: Dict[str, Any], sims_map: Dict[str, int]) -> Dict[str, Any]:
    """
    Adapts automatically to any number of connected SIMs (odd/even).
    - Excludes numbers already in ACTIVE conversations (avoids multi-conv per SIM)
    - Uses waiting_number to handle odd counts
    """
    numbers = sorted(list(sims_map.keys()))
    existing_pairs = build_existing_pairs_index(state)

    busy = set()
    for c in state.get("conversations", {}).values():
        if c.get("status") == "active":
            busy.add(c["a_number"])
            busy.add(c["b_number"])

    available = [n for n in numbers if n not in busy]

    waiting = state.get("waiting_number")
    if waiting and waiting in sims_map and waiting not in busy and waiting not in available:
        available.insert(0, waiting)
    state["waiting_number"] = None

    random.shuffle(available)

    created = 0
    created_ids = []
    skipped = 0

    i = 0
    while i + 1 < len(available):
        a, b = available[i], available[i + 1]
        i += 2

        key = conv_key(a, b)
        if key in existing_pairs:
            skipped += 1
            continue

        cid = start_one_conversation(state, a, b, sims_map)
        if cid:
            created += 1
            created_ids.append(cid)
            existing_pairs.add(key)
        else:
            skipped += 1

    if i < len(available):
        state["waiting_number"] = available[i]

    return {"created": created, "created_ids": created_ids, "skipped": skipped, "waiting_number": state["waiting_number"]}

def find_conv_id(state: Dict[str, Any], a: str, b: str) -> Optional[str]:
    for cid, c in state.get("conversations", {}).items():
        x, y = c["a_number"], c["b_number"]
        if (a == x and b == y) or (a == y and b == x):
            return cid
    return None

def my_sim(conv: dict, my_number: str) -> int:
    return int(conv["a_sim"]) if my_number == conv["a_number"] else int(conv["b_sim"])

# =========================
# Polling messages
# =========================

def fetch_received_messages() -> List[dict]:
    data = api_get("/api/v1/messages", params={
        "type": "SMS",
        "statuses[0]": "Received",
    })
    return data.get("data", [])

def process_inbound(state: Dict[str, Any], msg: dict) -> Optional[dict]:
    msg_id = int(msg.get("id") or 0)
    from_n = msg.get("from") or ""
    to_n = msg.get("to") or ""
    content = msg.get("content") or ""

    if not msg_id or not from_n or not to_n:
        return None

    dedupe = state.setdefault("dedupe_msg_ids", {})
    if str(msg_id) in dedupe:
        return {"ignored": "duplicate", "id": msg_id}
    dedupe[str(msg_id)] = time.time()

    # ignore our own tagged messages (avoid loops if platform echoes)
    if "[TEST conv=" in content:
        return {"ignored": "self_tagged", "id": msg_id}

    cid = find_conv_id(state, from_n, to_n)
    if not cid:
        return {"ignored": "no_match", "id": msg_id}

    conv = state["conversations"][cid]
    if conv["status"] != "active":
        return {"ignored": "inactive", "id": msg_id, "conv": cid}

    if int(conv["turn"]) >= int(conv["max_turns"]):
        conv["status"] = "done"
        return {"stopped": "max_turns", "conv": cid}

    responder = to_n
    responder_sim = my_sim(conv, responder)

    if not can_send(state, responder_sim):
        return {"skipped": "rate_limited", "conv": cid, "sim": responder_sim}

    next_turn = int(conv["turn"]) + 1
    reply_text = build_text(cid, next_turn)

    time.sleep(random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S))
    send_sms(responder_sim, from_n, reply_text)

    conv["turn"] = next_turn
    conv["last_sender"] = responder
    if int(conv["turn"]) >= int(conv["max_turns"]):
        conv["status"] = "done"

    return {"replied": True, "conv": cid, "turn": conv["turn"], "msg_id": msg_id}

# =========================
# Main loop
# =========================

def run():
    if not TOKEN:
        raise SystemExit("Missing RBSOFT_TOKEN environment variable.")

    print("RBSoft AutoChat (adaptive) started.")
    while True:
        try:
            # SIM refresh & adaptive pairing
            now = time.time()
            with _lock:
                state = load_state()
                last_refresh = float(state.get("meta", {}).get("last_sim_refresh", 0) or 0)

            if (now - last_refresh) >= SIM_REFRESH_INTERVAL_S:
                sims_map = fetch_sims()
                with _lock:
                    state = load_state()
                    state["known_sims"] = {k: int(v) for k, v in sims_map.items()}
                    state.setdefault("meta", {})["last_sim_refresh"] = now

                    pairing_result = adaptive_pairing(state, sims_map)
                    atomic_save(state)

                if pairing_result["created"] > 0 or pairing_result["waiting_number"]:
                    print("Pairing:", pairing_result)

            # Poll received messages and reply
            msgs = fetch_received_messages()
            msgs_sorted = sorted(msgs, key=lambda x: int(x.get("id") or 0))

            updates = []
            with _lock:
                state = load_state()
                for m in msgs_sorted:
                    out = process_inbound(state, m)
                    if out:
                        updates.append(out)
                atomic_save(state)

            if updates:
                print("Updates:", updates)

        except Exception as e:
            print("Error:", repr(e))

        time.sleep(POLL_INTERVAL_S)

if __name__ == "__main__":
    run()
