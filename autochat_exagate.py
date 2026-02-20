
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
    Logique de reponse robuste — ne depend PAS de deviceID/simSlot (souvent null).

    Principe :
      from_n  = celui qui a envoye le message (un de nos SIMs)
      On cherche dans convs la paire active contenant from_n
      L autre participant = celui qui repond

    Exemple : A a envoye a B (conv A|B, turn=1)
      B recoit → from_n=A → la paire active est A|B → le receiver est B → B repond a A
    """
    mid    = msg_id(msg)
    from_n = (msg.get("number") or "").strip()

    if not mid or not from_n:
        return None

    # Deduplication
    seen = state.setdefault("seen", {})
    if mid in seen:
        return None
    seen[mid] = time.time()

    sims = state.get("sims", {})

    # Ignorer les messages d expediteurs qui ne sont pas nos SIMs
    if from_n not in sims:
        return None

    # Chercher la conv active ou from_n est participant
    convs = state.setdefault("convs", {})
    matched_pk   = None
    receiver_num = None

    for pk, conv in convs.items():
        if conv.get("status") != "active":
            continue
        parts = pk.split("|", 1)
        if len(parts) != 2:
            continue
        a, b = parts
        if from_n == a and b in sims:
            matched_pk, receiver_num = pk, b
            break
        if from_n == b and a in sims:
            matched_pk, receiver_num = pk, a
            break

    if not matched_pk:
        # Aucune conv active trouvee pour from_n
        active_convs = {pk: c for pk, c in convs.items() if c.get("status") == "active"}
        print(f"  [DEBUG process] SKIP no_active_conv from={from_n!r} active_convs={list(active_convs.keys())}", flush=True)
        return {"skip": "no_active_conv", "from": from_n}

    receiver_spec = sims.get(receiver_num)
    if not receiver_spec:
        return {"skip": "no_spec", "receiver": receiver_num}

    conv = convs[matched_pk]
    turn = int(conv.get("turn", 1))

    if turn >= MAX_TURNS:
        conv["status"] = "done"
        print(f"  [DONE] {matched_pk}", flush=True)
        return {"done": matched_pk}

    if not can_send(state, receiver_spec):
        return {"skip": "rate", "pk": matched_pk}

    next_turn = turn + 1
    reply     = tpl(next_turn)

    delay = random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S)
    time.sleep(delay)

    try:
        send_sms(receiver_spec, from_n, reply)
        conv["turn"] = next_turn
        conv["at"]   = time.time()
        if next_turn >= MAX_TURNS:
            conv["status"] = "done"
            print(f"  [DONE] {matched_pk} ({next_turn} tours)", flush=True)
        # Sync paire rr
        for ppk in [f"{from_n}|{receiver_num}", f"{receiver_num}|{from_n}"]:
            if ppk in state.get("pairs", {}):
                state["pairs"][ppk]["turn"] = next_turn
                if next_turn >= MAX_TURNS:
                    state["pairs"][ppk]["status"] = "done"
        print(f"  [REPLY] {receiver_num} -> {from_n} turn={next_turn}: {reply[:40]}", flush=True)
        return {"replied": matched_pk, "turn": next_turn}
    except Exception as e:
        return {"err": str(e), "pk": matched_pk}

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

                # DEBUG : afficher tous les messages bruts non encore vus
                seen_ids = state.get("seen", {})
                new_msgs = [m for m in msgs if msg_id(m) not in seen_ids]
                if new_msgs:
                    print(f"[DEBUG] {len(new_msgs)} nouveaux msgs:", flush=True)
                    for m in new_msgs:
                        print(f"  msg brut: number={m.get('number')!r} deviceID={m.get('deviceID')!r} simSlot={m.get('simSlot')!r} id={m.get('id') or m.get('ID')!r} msg={str(m.get('message',''))[:40]!r}", flush=True)
                    print(f"  sims connus: {sorted(state.get('sims',{}).keys())}", flush=True)
                    print(f"  convs: {list(state.get('convs',{}).keys())}", flush=True)

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
