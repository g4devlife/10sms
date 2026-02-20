
import os, re, json, time, random, tempfile
from typing import Dict, Any, List, Optional
import requests

BASE_URL = (os.getenv("SMS_GATEWAY_URL") or "https://gate.exanewtech.com").rstrip("/")
API_KEY  = os.getenv("SMS_GATEWAY_API_KEY") or os.getenv("RBSOFT_TOKEN") or ""

STATE_FILE           = os.getenv("STATE_FILE",            "rbsoft_state.json")
MAX_TURNS            = int(os.getenv("MAX_TURNS",             "10"))
POLL_INTERVAL_S      = int(os.getenv("POLL_INTERVAL_S",       "5"))
SIM_REFRESH_S        = int(os.getenv("SIM_REFRESH_INTERVAL_S","60"))
REPLY_DELAY_MIN_S    = int(os.getenv("REPLY_DELAY_MIN_S",     "3"))
REPLY_DELAY_MAX_S    = int(os.getenv("REPLY_DELAY_MAX_S",     "5"))
GLOBAL_SEND_PER_MIN  = int(os.getenv("GLOBAL_SEND_PER_MIN",  "60"))
PER_SIM_SEND_PER_MIN = int(os.getenv("PER_SIM_SEND_PER_MIN", "20"))
RR_TICK_S            = int(os.getenv("RR_TICK_S",             "20"))

EP_DEVICES  = "/services/get-devices.php"
EP_SEND     = "/services/send.php"
EP_MESSAGES = "/services/get-messages.php"

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
        print(f"[WARN] bad JSON ({ctx}) {r.status_code} => {body[:100]!r}", flush=True)
        return {}

def api_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=_h(),
                     params={**_p(), **(params or {})}, timeout=30)
    r.raise_for_status()
    return _json(r, path)

# ─── SIMs ───────────────────────────────────────────────────────────────────────
_NUM_RE  = re.compile(r"\[([^\]]+)\]")
_PHONE_RE = re.compile(r"^\+\d{7,15}$")   # E.164 strict

def _is_phone(s: str) -> bool:
    """Retourne True uniquement si s ressemble a un vrai numero E.164."""
    return bool(_PHONE_RE.match(s.strip()))

def fetch_sims():
    data = api_get(EP_DEVICES)
    out  = {}
    skipped = []
    for dev in (data.get("data") or {}).get("devices", []):
        did = dev.get("id")
        for slot, label in (dev.get("sims") or {}).items():
            m = _NUM_RE.search(label)
            candidate = m.group(1).strip() if m else label.strip()
            if did and _is_phone(candidate):
                out[candidate] = f"{did}|{slot}"
            else:
                skipped.append(label)
    if skipped:
        print(f"[SIMS] Ignores (pas de numero valide): {skipped}", flush=True)
    return out

# ─── SEND ───────────────────────────────────────────────────────────────────────
def send_sms(spec, to, msg):
    params = {"number": to, "message": msg, "devices": spec, "type": "sms", "prioritize": 1}
    r = requests.get(f"{BASE_URL}{EP_SEND}", headers=_h(),
                     params={**_p(), **params}, timeout=30)
    r.raise_for_status()
    d = _json(r, EP_SEND)
    if isinstance(d, dict) and d.get("success") is False:
        err = d.get("error", {})
        raise RuntimeError((err.get("message") if isinstance(err, dict) else str(err)))
    print(f"  [SMS] {spec} -> {to}: {msg[:55]}", flush=True)

# ─── MESSAGES RECUS ─────────────────────────────────────────────────────────────
def fetch_received():
    d = api_get(EP_MESSAGES, {"status": "Received"})
    if not d or not d.get("success"):
        return []
    return (d.get("data") or {}).get("messages", [])

def msg_id(m):
    mid = m.get("id") or m.get("ID")
    if mid:
        return str(mid)
    return f"{m.get('number','')}-{m.get('message','')}-{m.get('sentDate','')}"

# ─── STATE ──────────────────────────────────────────────────────────────────────
def blank():
    return {
        "pairs":   {},    # {numA|numB: {sender,target,turn,status}}
        "routing": {},    # {numB: numA}
        "rr_idx":  0,     # index emetteur courant
        "sims":    {},    # {number: spec}
        "seen":    {},    # {msg_id: timestamp} dedup
        "rate":    {"global": [], "per": {}},
        "meta":    {"last_refresh": 0},
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
    d = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
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

def can_send(state, spec):
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
def tpl(turn):
    return TEMPLATES[(turn - 1) % len(TEMPLATES)]

def cur_sender(state, sims_list):
    return sims_list[state.get("rr_idx", 0) % len(sims_list)]

def sender_done(state, sender, sims_list):
    targets = [n for n in sims_list if n != sender]
    if not targets:
        return True
    pairs = state.get("pairs", {})
    return all(pairs.get(f"{sender}|{t}", {}).get("status") == "done" for t in targets)

def advance_rr(state, sims_list):
    old = state.get("rr_idx", 0)
    new = (old + 1) % len(sims_list)
    state["rr_idx"] = new
    if new == 0:
        state["pairs"]   = {}
        state["routing"] = {}
        print("[RR] Cycle complet — nouveau cycle", flush=True)
    ns = sims_list[new]
    print(f"[RR] Emetteur -> {ns} (idx={new})", flush=True)
    return ns

def rr_tick(state):
    sims = state.get("sims", {})
    if len(sims) < 2:
        return {"skip": "not_enough_sims"}

    sims_list = sorted(sims.keys())
    sender    = cur_sender(state, sims_list)

    if sender_done(state, sender, sims_list):
        print(f"[RR] {sender} termine, passage au suivant", flush=True)
        sender = advance_rr(state, sims_list)

    spec    = sims.get(sender)
    targets = [n for n in sims_list if n != sender]
    sent = skip = 0

    for target in targets:
        pk   = f"{sender}|{target}"
        pair = state.get("pairs", {}).get(pk)

        if pair is None:
            if not can_send(state, spec):
                skip += 1
                continue
            msg = tpl(1)
            try:
                send_sms(spec, target, msg)
                state.setdefault("pairs", {})[pk] = {
                    "sender": sender, "target": target,
                    "turn": 1, "status": "active", "at": time.time()
                }
                state.setdefault("routing", {})[target] = sender
                # Synchro convs (cle symetrique) pour process()
                cpk = "|".join(sorted([sender, target]))
                state.setdefault("convs", {})[cpk] = {
                    "turn": 1, "status": "active", "at": time.time()
                }
                sent += 1
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                print(f"  [ERR send] {sender}->{target}: {e}", flush=True)
                # Marquer done pour ne pas reessayer en boucle
                state.setdefault("pairs", {})[pk] = {
                    "sender": sender, "target": target,
                    "turn": 0, "status": "done", "err": str(e), "at": time.time()
                }
                skip += 1
        elif pair.get("status") == "done":
            skip += 1

    active = sum(1 for p in state.get("pairs", {}).values() if p.get("status") == "active")
    return {"sender": sender, "sent": sent, "skip": skip, "active": active}

# ─── INBOUND ────────────────────────────────────────────────────────────────────
def process(state, msg):
    """
    Logique simplifiee basee sur deviceID+simSlot :
      - from_n    = numero qui nous a envoye le message
      - receiver  = notre SIM qui a recu (identifie via deviceID+simSlot)
      - On repond DEPUIS receiver VERS from_n
    Fonctionne quel que soit le sens de la conversation.
    """
    mid    = msg_id(msg)
    from_n = (msg.get("number") or "").strip()
    dev_id = msg.get("deviceID")
    slot   = msg.get("simSlot")

    if not mid or not from_n:
        return None

    # Deduplication
    seen = state.setdefault("seen", {})
    if mid in seen:
        return None
    seen[mid] = time.time()

    sims = state.get("sims", {})

    # L expediteur doit etre un de nos SIMs (conversation inter-SIM uniquement)
    if from_n not in sims:
        return None  # message externe, ignorer silencieusement

    # Identifier quel SIM a recu via deviceID+simSlot
    receiver_spec = None
    receiver_num  = None
    if dev_id is not None and slot is not None:
        spec_candidate = f"{dev_id}|{slot}"
        for num, spec in sims.items():
            if spec == spec_candidate:
                receiver_spec = spec
                receiver_num  = num
                break

    # Fallback si deviceID/simSlot absent : chercher via routing ou pairs
    if not receiver_spec:
        routing = state.get("routing", {})
        sender_via_routing = routing.get(from_n)
        if sender_via_routing and sender_via_routing in sims:
            receiver_num  = sender_via_routing
            receiver_spec = sims[sender_via_routing]

    if not receiver_spec or not receiver_num:
        return {"skip": "cant_identify_receiver", "from": from_n,
                "deviceID": dev_id, "simSlot": slot}

    # Cle de paire symetrique : toujours trie pour eviter duplicates
    pk   = "|".join(sorted([from_n, receiver_num]))
    convs = state.setdefault("convs", {})
    conv  = convs.get(pk)

    if conv is None:
        # Nouvelle conversation non initiee par rr_tick (message entrant spontane)
        conv = {"turn": 0, "status": "active"}

    if conv.get("status") == "done":
        return {"skip": "conv_done", "pk": pk}

    turn = int(conv.get("turn", 0))
    if turn >= MAX_TURNS:
        conv["status"] = "done"
        convs[pk] = conv
        print(f"  [DONE] {pk} ({turn} tours)", flush=True)
        return {"done": pk}

    if not can_send(state, receiver_spec):
        return {"skip": "rate", "pk": pk}

    next_turn = turn + 1
    reply     = tpl(next_turn)

    # Delai humain avant reponse
    delay = random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S)
    print(f"  [WAIT {delay}s] {receiver_num} -> {from_n}", flush=True)
    time.sleep(delay)

    try:
        send_sms(receiver_spec, from_n, reply)
        conv["turn"]   = next_turn
        conv["at"]     = time.time()
        if next_turn >= MAX_TURNS:
            conv["status"] = "done"
            print(f"  [DONE] {pk} ({next_turn} tours)", flush=True)
        convs[pk] = conv
        # Mettre a jour aussi la paire rr si elle existe
        pairs = state.get("pairs", {})
        for ppk in [f"{from_n}|{receiver_num}", f"{receiver_num}|{from_n}"]:
            if ppk in pairs and pairs[ppk].get("status") == "active":
                pairs[ppk]["turn"] = next_turn
                if next_turn >= MAX_TURNS:
                    pairs[ppk]["status"] = "done"
        return {"replied": pk, "turn": next_turn,
                "from": from_n, "via": receiver_num}
    except Exception as e:
        return {"err": str(e), "pk": pk}

# ─── MAIN ───────────────────────────────────────────────────────────────────────
def run():
    if not API_KEY:
        raise SystemExit("SMS_GATEWAY_API_KEY manquant.")

    print("AutoChat ExaGate — Round-Robin Broadcast", flush=True)
    print(f"BASE_URL = {BASE_URL}", flush=True)

    # Test connexion
    try:
        r = requests.get(f"{BASE_URL}{EP_DEVICES}", headers=_h(), params=_p(), timeout=10)
        print(f"[INIT] connexion OK -> {r.status_code}", flush=True)
    except Exception as e:
        raise SystemExit(f"Connexion impossible : {e}")

    # Charger etat
    state = blank() if os.getenv("RESET_STATE", "0") == "1" else load_state()
    if os.getenv("RESET_STATE", "0") == "1":
        print("[INIT] RESET_STATE=1 — etat vierge", flush=True)

    # Recuperer SIMs
    try:
        sims = fetch_sims()
        if len(sims) < 2:
            raise SystemExit(f"Seulement {len(sims)} SIM(s), minimum 2.")
        state["sims"] = sims
        save_state(state)
        print(f"[INIT] {len(sims)} SIMs :", flush=True)
        for n, s in sorted(sims.items()):
            print(f"  {n} -> {s}", flush=True)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Erreur SIMs : {e}")

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
                        state["sims"] = fresh
                        last_refresh  = now
                        print(f"[SIMS] {len(fresh)}: {sorted(fresh.keys())}", flush=True)
                except Exception as e:
                    print(f"[WARN sims] {e}", flush=True)

            sims = state.get("sims", {})
            if len(sims) < 2:
                print("[WARN] < 2 SIMs, attente...", flush=True)
                time.sleep(15)
                continue

            # Messages entrants
            try:
                msgs = sorted(fetch_received(),
                              key=lambda x: int(x.get("id") or x.get("ID") or 0))
                results = [r for m in msgs if (r := process(state, m)) is not None]
                if results:
                    print(f"[IN] {results}", flush=True)
            except Exception as e:
                print(f"[ERR inbound] {e}", flush=True)

            # Tick round-robin
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
            print(f"[ERR] {repr(e)}", flush=True)

        time.sleep(POLL_INTERVAL_S)

if __name__ == "__main__":
    run()
