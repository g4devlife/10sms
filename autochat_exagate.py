"""
AutoChat ExaGate — Round-Robin Broadcast
API self-hosted: gate.exanewtech.com
Auth: ?key=API_KEY
Endpoints: /services/get-devices.php  /services/send.php  /services/get-messages.php
"""
import os, re, json, time, random, tempfile
from typing import Dict, List, Optional
import requests

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL  = (os.getenv("SMS_GATEWAY_URL") or "https://gate.exanewtech.com").rstrip("/")
API_KEY   = os.getenv("SMS_GATEWAY_API_KEY") or os.getenv("RBSOFT_TOKEN") or ""

STATE_FILE           = os.getenv("STATE_FILE",             "rbsoft_state.json")
MAX_TURNS            = int(os.getenv("MAX_TURNS",              "10"))
POLL_INTERVAL_S      = int(os.getenv("POLL_INTERVAL_S",        "5"))
SIM_REFRESH_S        = int(os.getenv("SIM_REFRESH_INTERVAL_S", "120"))
REPLY_DELAY_MIN_S    = int(os.getenv("REPLY_DELAY_MIN_S",      "3"))
REPLY_DELAY_MAX_S    = int(os.getenv("REPLY_DELAY_MAX_S",      "5"))
GLOBAL_SEND_PER_MIN  = int(os.getenv("GLOBAL_SEND_PER_MIN",    "60"))
PER_SIM_SEND_PER_MIN = int(os.getenv("PER_SIM_SEND_PER_MIN",   "20"))
RR_TICK_S            = int(os.getenv("RR_TICK_S",              "20"))

TEMPLATES = [
    "Hello !",
    "Ca va de ton cote ?",
    "Tu fais quoi en ce moment ?",
    "La journee s est bien passee ?",
    "Tu as mange ?",
    "Des nouvelles a partager ?",
    "Tu bosses sur quoi ces derniers temps ?",
    "Toujours la ?",
    "On se capte bientot !",
    "Prends soin de toi.",
]

# ─── HTTP ───────────────────────────────────────────────────────────────────────
def _p():
    return {"key": API_KEY}

def _h():
    return {"Accept": "application/json"}

def _json(r, ctx=""):
    body = (r.text or "").strip()
    if not body:
        return {}
    try:
        return r.json()
    except ValueError:
        print(f"[WARN] bad JSON ({ctx}) {r.status_code}: {body[:100]!r}", flush=True)
        return {}

def api_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=_h(),
                     params={**_p(), **(params or {})}, timeout=30)
    r.raise_for_status()
    return _json(r, path)

# ─── SIMs ───────────────────────────────────────────────────────────────────────
_NUM_RE   = re.compile(r"\[([^\]]+)\]")
_PHONE_RE = re.compile(r"^\+\d{7,15}$")

def fetch_sims() -> Dict[str, str]:
    """Retourne {phone: 'device_id|slot'}"""
    data = api_get("/services/get-devices.php")
    out  = {}
    skip = []
    for dev in (data.get("data") or {}).get("devices", []):
        did = dev.get("id")
        for slot, label in (dev.get("sims") or {}).items():
            m   = _NUM_RE.search(label)
            num = m.group(1).strip() if m else label.strip()
            if did and _PHONE_RE.match(num):
                out[num] = f"{did}|{slot}"
            else:
                skip.append(label)
    if skip:
        print(f"[SIMS] Ignores: {skip}", flush=True)
    return out

# ─── SEND ───────────────────────────────────────────────────────────────────────
def send_sms(spec: str, to: str, msg: str):
    """GET /services/send.php?key=...&number=...&message=...&devices=DEVICE_ID|SLOT"""
    r = requests.get(f"{BASE_URL}/services/send.php", headers=_h(), params={
        **_p(), "number": to, "message": msg,
        "devices": spec, "type": "sms", "prioritize": 1
    }, timeout=30)
    r.raise_for_status()
    d = _json(r, "send")
    if isinstance(d, dict) and d.get("success") is False:
        err = d.get("error", {})
        raise RuntimeError((err.get("message") if isinstance(err, dict) else str(err)))
    print(f"  [SMS] {spec} -> {to}: {msg[:55]}", flush=True)

# ─── MESSAGES RECUS ─────────────────────────────────────────────────────────────
def fetch_received() -> List[dict]:
    """
    GET /services/get-messages.php?status=Received
    Retourne: [{id, number (expediteur), message, deviceID, simSlot}, ...]
    """
    d = api_get("/services/get-messages.php", {"status": "Received"})
    if not d or not d.get("success"):
        return []
    msgs = (d.get("data") or {}).get("messages", [])
    return msgs

def msg_id(m: dict) -> str:
    mid = m.get("id") or m.get("ID")
    if mid:
        return str(mid)
    return f"{m.get('number','')}-{m.get('message','')}-{m.get('sentDate','')}"

# ─── STATE ──────────────────────────────────────────────────────────────────────
def blank():
    return {
        "convs":  {},   # {sortedA|sortedB: {turn, status, last_sender}}
        "rr_idx": 0,
        "sims":   {},   # {phone: "dev|slot"}
        "seen":   {},   # {msg_id: ts}
        "rate":   {"global": [], "per": {}},
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return blank()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return blank()

def save_state(state):
    d   = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    fd, tmp = tempfile.mkstemp(prefix="rbs_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] save: {e}", flush=True)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass

# ─── RATE LIMIT ─────────────────────────────────────────────────────────────────
def _prune(lst, w=60):
    now = time.time()
    return [t for t in lst if now - t <= w]

def can_send(state, spec: str) -> bool:
    rate = state.setdefault("rate", {"global": [], "per": {}})
    rate["global"] = _prune(rate.get("global", []))
    per = rate.setdefault("per", {})
    per[spec] = _prune(per.get(spec, []))
    if len(rate["global"]) >= GLOBAL_SEND_PER_MIN:
        return False
    if len(per.get(spec, [])) >= PER_SIM_SEND_PER_MIN:
        return False
    rate["global"].append(time.time())
    per.setdefault(spec, []).append(time.time())
    return True

# ─── ROUND-ROBIN ────────────────────────────────────────────────────────────────
def tpl(turn: int) -> str:
    return TEMPLATES[(turn - 1) % len(TEMPLATES)]

def ck(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))

def cur_sender(state, sims_list) -> str:
    return sims_list[state.get("rr_idx", 0) % len(sims_list)]

def sender_done(state, sender: str, sims_list) -> bool:
    targets = [n for n in sims_list if n != sender]
    if not targets:
        return True
    convs = state.get("convs", {})
    return all(convs.get(ck(sender, t), {}).get("status") == "done" for t in targets)

def advance_rr(state, sims_list) -> str:
    new_idx = (state.get("rr_idx", 0) + 1) % len(sims_list)
    state["rr_idx"] = new_idx
    if new_idx == 0:
        state["convs"] = {}
        state["seen"]  = {}
        print("[RR] Nouveau cycle complet", flush=True)
    ns = sims_list[new_idx]
    print(f"[RR] Emetteur suivant -> {ns} (idx={new_idx})", flush=True)
    return ns

def rr_tick(state) -> dict:
    sims = state.get("sims", {})
    if len(sims) < 2:
        return {"skip": "not_enough_sims"}

    sims_list = sorted(sims.keys())
    sender    = cur_sender(state, sims_list)

    if sender_done(state, sender, sims_list):
        print(f"[RR] {sender} termine", flush=True)
        sender = advance_rr(state, sims_list)

    spec    = sims[sender]
    targets = [n for n in sims_list if n != sender]
    sent = skip = 0

    for target in targets:
        key  = ck(sender, target)
        conv = state.get("convs", {}).get(key)

        if conv is None:
            # Premier envoi
            if not can_send(state, spec):
                skip += 1
                continue
            try:
                send_sms(spec, target, tpl(1))
                state.setdefault("convs", {})[key] = {
                    "turn": 1, "status": "active",
                    "last_sender": sender, "at": time.time()
                }
                sent += 1
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                print(f"  [ERR] {sender}->{target}: {e}", flush=True)
                state.setdefault("convs", {})[key] = {
                    "turn": 0, "status": "done", "err": str(e)
                }
                skip += 1
        elif conv.get("status") == "done":
            skip += 1

    active = sum(1 for c in state.get("convs", {}).values() if c.get("status") == "active")
    return {"sender": sender, "sent": sent, "skip": skip, "active": active}

# ─── MESSAGES ENTRANTS ──────────────────────────────────────────────────────────
def process(state, msg: dict):
    """
    get-messages.php retourne:
      number   = expediteur (un de nos SIMs)
      deviceID = device qui a recu
      simSlot  = slot du SIM recepteur

    On repond DEPUIS le SIM recepteur VERS l expediteur.
    """
    mid      = msg_id(msg)
    from_num = (msg.get("number") or "").strip()
    dev_id   = msg.get("deviceID")
    slot     = msg.get("simSlot")

    if not mid or not from_num:
        return None

    # Deduplication
    seen = state.setdefault("seen", {})
    if mid in seen:
        return None
    seen[mid] = time.time()

    sims = state.get("sims", {})

    # L expediteur doit etre un de nos SIMs
    if from_num not in sims:
        return None

    # Identifier le SIM recepteur via deviceID+simSlot
    receiver_spec = receiver_num = None
    if dev_id is not None and slot is not None:
        candidate = f"{dev_id}|{slot}"
        for num, spec in sims.items():
            if spec == candidate:
                receiver_spec, receiver_num = spec, num
                break

    # Fallback: chercher la conv active qui implique from_num
    if not receiver_spec:
        convs = state.get("convs", {})
        for k, conv in convs.items():
            if conv.get("status") != "active":
                continue
            parts = k.split("|", 1)
            if len(parts) != 2:
                continue
            a, b = parts
            if from_num == a and b in sims:
                receiver_num, receiver_spec = b, sims[b]
                break
            if from_num == b and a in sims:
                receiver_num, receiver_spec = a, sims[a]
                break

    if not receiver_spec or not receiver_num:
        print(f"  [SKIP] cant identify receiver for from={from_num} dev={dev_id} slot={slot}", flush=True)
        return {"skip": "no_receiver", "from": from_num}

    key  = ck(from_num, receiver_num)
    convs = state.setdefault("convs", {})
    conv  = convs.get(key)

    if conv is None:
        conv = {"turn": 1, "status": "active",
                "last_sender": from_num, "at": time.time()}
        convs[key] = conv

    if conv.get("status") == "done":
        return {"skip": "done", "key": key}

    turn = int(conv.get("turn", 1))
    if turn >= MAX_TURNS:
        conv["status"] = "done"
        print(f"  [DONE] {key}", flush=True)
        return {"done": key}

    if not can_send(state, receiver_spec):
        return {"skip": "rate", "key": key}

    next_turn = turn + 1
    reply     = tpl(next_turn)

    delay = random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S)
    time.sleep(delay)

    try:
        send_sms(receiver_spec, from_num, reply)
        conv["turn"]        = next_turn
        conv["last_sender"] = receiver_num
        conv["at"]          = time.time()
        if next_turn >= MAX_TURNS:
            conv["status"] = "done"
            print(f"  [DONE] {key}", flush=True)
        print(f"  [REPLY] {receiver_num} -> {from_num} tour={next_turn}", flush=True)
        return {"replied": key, "turn": next_turn}
    except Exception as e:
        return {"err": str(e), "key": key}

# ─── MAIN ───────────────────────────────────────────────────────────────────────
def run():
    if not API_KEY:
        raise SystemExit("SMS_GATEWAY_API_KEY manquant.")

    print("AutoChat ExaGate v4 — Round-Robin Broadcast", flush=True)
    print(f"BASE_URL = {BASE_URL}", flush=True)

    # Test connexion
    try:
        r = requests.get(f"{BASE_URL}/services/get-devices.php",
                         headers=_h(), params=_p(), timeout=10)
        print(f"[INIT] connexion -> {r.status_code}", flush=True)
        if r.status_code != 200:
            print(f"[INIT] body: {r.text[:200]}", flush=True)
    except Exception as e:
        raise SystemExit(f"Connexion impossible: {e}")

    # Charger etat (toujours reset seen au demarrage)
    state = blank() if os.getenv("RESET_STATE", "0") == "1" else load_state()
    state["seen"] = {}  # toujours vider seen au demarrage pour ne pas rater de msgs
    if os.getenv("RESET_STATE", "0") == "1":
        print("[INIT] RESET_STATE=1 — etat vierge", flush=True)
    else:
        print("[INIT] seen vide, convs conservees", flush=True)

    # Recuperer SIMs
    try:
        sims = fetch_sims()
        if len(sims) < 2:
            raise SystemExit(f"Seulement {len(sims)} SIM(s), minimum 2.")
        # Purger convs avec numeros invalides
        valid = set(sims.keys())
        state["convs"] = {k: v for k, v in state.get("convs", {}).items()
                          if all(p in valid for p in k.split("|", 1))}
        state["sims"]  = sims
        save_state(state)
        print(f"[INIT] {len(sims)} SIMs valides:", flush=True)
        for num, spec in sorted(sims.items()):
            print(f"  {num} -> {spec}", flush=True)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Erreur SIMs: {e}")

    print(f"\nDemarrage dans 3s... MAX_TURNS={MAX_TURNS}\n", flush=True)
    time.sleep(3)

    last_refresh = 0.0
    last_tick    = 0.0

    while True:
        try:
            now = time.time()

            # Rafraichissement SIMs
            if now - last_refresh >= SIM_REFRESH_S:
                try:
                    fresh = fetch_sims()
                    if fresh:
                        valid = set(fresh.keys())
                        state["convs"] = {k: v for k, v in state.get("convs", {}).items()
                                          if all(p in valid for p in k.split("|", 1))}
                        state["sims"] = fresh
                        last_refresh  = now
                        print(f"[SIMS] {len(fresh)}: {sorted(fresh.keys())}", flush=True)
                except Exception as e:
                    print(f"[WARN refresh] {e}", flush=True)

            sims = state.get("sims", {})
            if len(sims) < 2:
                time.sleep(15)
                continue

            # ── Messages entrants → reponse tac-a-tac ─────────────────────
            try:
                msgs = sorted(fetch_received(),
                              key=lambda x: int(x.get("id") or x.get("ID") or 0))

                seen_ids = state.get("seen", {})
                new_msgs = [m for m in msgs if msg_id(m) not in seen_ids]

                if new_msgs:
                    print(f"[INBOUND] {len(new_msgs)} nouveau(x) sur {len(msgs)} total", flush=True)
                    for m in new_msgs:
                        print(f"  from={m.get('number')!r} dev={m.get('deviceID')} "
                              f"slot={m.get('simSlot')} id={m.get('id') or m.get('ID')} "
                              f"msg={str(m.get('message',''))[:40]!r}", flush=True)

                results = []
                for m in msgs:
                    r = process(state, m)
                    if r is not None:
                        results.append(r)
                if results:
                    print(f"[IN] {results}", flush=True)

            except Exception as e:
                import traceback
                print(f"[ERR inbound] {e}", flush=True)
                traceback.print_exc()

            # ── Tick round-robin ───────────────────────────────────────────
            if now - last_tick >= RR_TICK_S:
                try:
                    rr = rr_tick(state)
                    last_tick = now
                    if rr.get("sent", 0) > 0 or rr.get("active", 0) > 0:
                        print(f"[RR] {rr}", flush=True)
                except Exception as e:
                    print(f"[ERR tick] {e}", flush=True)

            save_state(state)

        except Exception as e:
            import traceback
            print(f"[ERR loop] {repr(e)}", flush=True)
            traceback.print_exc()

        time.sleep(POLL_INTERVAL_S)

if __name__ == "__main__":
    run()
