"""
AutoChat RBSoft SMS Gateway — Round-Robin Broadcast
=====================================================
API: https://smsgateway.rbsoft.org/api/v1/
Auth: Authorization: Bearer TOKEN

Logique:
  SIM[0] envoie a tous les autres → chacun repond automatiquement (tac-a-tac)
  Quand SIM[0] a termine → SIM[1] envoie a tous → cycle infini
"""
import os, re, json, time, random, tempfile
from typing import Dict, List, Optional
import requests

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL  = (os.getenv("SMS_GATEWAY_URL") or "https://gate.exanewtech.com").rstrip("/")
API_TOKEN = os.getenv("SMS_GATEWAY_API_KEY") or os.getenv("RBSOFT_TOKEN") or ""

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
def _headers():
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

def _json(r, ctx=""):
    body = (r.text or "").strip()
    if not body:
        return {}
    try:
        return r.json()
    except ValueError:
        print(f"[WARN] bad JSON ({ctx}) {r.status_code}: {body[:120]!r}", flush=True)
        return {}

def api_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=_headers(),
                     params=params or {}, timeout=30)
    r.raise_for_status()
    return _json(r, path)

def api_post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=_headers(),
                      json=payload, timeout=30)
    r.raise_for_status()
    return _json(r, path)

# ─── SIMs ───────────────────────────────────────────────────────────────────────
_PHONE_RE = re.compile(r"^\+\d{7,15}$")

def fetch_sims() -> Dict[str, int]:
    """
    GET /api/v1/devices
    Retourne {phone_number: sim_id}
    sim_id est l entier utilise dans messages/send
    """
    data = api_get("/api/v1/devices")
    out  = {}
    skip = []
    for dev in (data.get("data") or []):
        for sim in (dev.get("sims") or []):
            num    = (sim.get("number") or "").strip()
            sim_id = sim.get("id")
            if sim_id and num and _PHONE_RE.match(num):
                out[num] = sim_id
            else:
                skip.append(sim.get("number") or sim.get("name") or "?")
    if skip:
        print(f"[SIMS] Ignores: {skip}", flush=True)
    return out

# ─── SEND ───────────────────────────────────────────────────────────────────────
def send_sms(sim_id: int, to: str, msg: str):
    """POST /api/v1/messages/send"""
    payload = {
        "sims":           [sim_id],
        "mobile_numbers": [to],
        "message":        msg,
        "type":           "SMS",
    }
    resp = api_post("/api/v1/messages/send", payload)
    # succes si on obtient un id de campagne ou un objet
    if isinstance(resp, dict) and resp.get("id"):
        print(f"  [SMS] sim#{sim_id} -> {to}: {msg[:55]}", flush=True)
        return
    # certaines versions retournent juste {}
    print(f"  [SMS] sim#{sim_id} -> {to}: {msg[:55]} (resp={resp})", flush=True)

# ─── MESSAGES RECUS ─────────────────────────────────────────────────────────────
def fetch_received() -> List[dict]:
    """
    GET /api/v1/messages?statuses[0]=Received
    Retourne liste avec champs: id, from, to, content
    """
    data = api_get("/api/v1/messages", {"statuses[0]": "Received"})
    return (data.get("data") or [])

def msg_id(m: dict) -> str:
    mid = m.get("id")
    if mid:
        return str(mid)
    return f"{m.get('from','')}-{m.get('to','')}-{m.get('content','')[:20]}"

# ─── STATE ──────────────────────────────────────────────────────────────────────
def blank():
    return {
        # {numA|numB: {turn, status}}  cle triee alphabetiquement
        "convs":  {},
        # index du SIM emetteur courant dans round-robin
        "rr_idx": 0,
        # {phone_number: sim_id}
        "sims":   {},
        # {msg_id: timestamp} deduplication
        "seen":   {},
        # rate limit
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

def can_send(state, sim_id: int) -> bool:
    key  = str(sim_id)
    rate = state.setdefault("rate", {"global": [], "per": {}})
    rate["global"] = _prune(rate.get("global", []))
    per = rate.setdefault("per", {})
    per[key] = _prune(per.get(key, []))
    if len(rate["global"]) >= GLOBAL_SEND_PER_MIN:
        return False
    if len(per.get(key, [])) >= PER_SIM_SEND_PER_MIN:
        return False
    rate["global"].append(time.time())
    per.setdefault(key, []).append(time.time())
    return True

# ─── ROUND-ROBIN ────────────────────────────────────────────────────────────────
def tpl(turn: int) -> str:
    return TEMPLATES[(turn - 1) % len(TEMPLATES)]

def conv_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))

def current_sender(state, sims_list) -> str:
    return sims_list[state.get("rr_idx", 0) % len(sims_list)]

def sender_done(state, sender: str, sims_list) -> bool:
    targets = [n for n in sims_list if n != sender]
    if not targets:
        return True
    convs = state.get("convs", {})
    return all(convs.get(conv_key(sender, t), {}).get("status") == "done"
               for t in targets)

def advance_rr(state, sims_list) -> str:
    new_idx = (state.get("rr_idx", 0) + 1) % len(sims_list)
    state["rr_idx"] = new_idx
    if new_idx == 0:
        # Nouveau cycle : remettre toutes les convs a zero
        state["convs"] = {}
        print("[RR] Nouveau cycle — convs remises a zero", flush=True)
    ns = sims_list[new_idx]
    print(f"[RR] Emetteur -> {ns} (idx={new_idx})", flush=True)
    return ns

def rr_tick(state) -> dict:
    """Envoie le premier message de l emetteur courant vers chaque target."""
    sims = state.get("sims", {})
    if len(sims) < 2:
        return {"skip": "not_enough_sims"}

    sims_list = sorted(sims.keys())
    sender    = current_sender(state, sims_list)

    if sender_done(state, sender, sims_list):
        print(f"[RR] {sender} a termine, passage au suivant", flush=True)
        sender = advance_rr(state, sims_list)

    sender_id = sims.get(sender)
    targets   = [n for n in sims_list if n != sender]
    sent = skip = 0

    for target in targets:
        ck   = conv_key(sender, target)
        conv = state.get("convs", {}).get(ck)

        if conv is None:
            # Premier envoi vers ce target
            if not can_send(state, sender_id):
                skip += 1
                continue
            msg = tpl(1)
            try:
                send_sms(sender_id, target, msg)
                state.setdefault("convs", {})[ck] = {
                    "turn":         1,
                    "status":       "active",
                    "last_sender":  sender,
                    "at":           time.time(),
                }
                sent += 1
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                print(f"  [ERR] {sender}->{target}: {e}", flush=True)
                # Marquer done pour eviter boucle infinie
                state.setdefault("convs", {})[ck] = {
                    "turn": 0, "status": "done", "err": str(e)
                }
                skip += 1

        elif conv.get("status") == "done":
            skip += 1   # terminee, attendre avancement rr

    active = sum(1 for c in state.get("convs", {}).values()
                 if c.get("status") == "active")
    return {"sender": sender, "sent": sent, "skip": skip, "active": active}

# ─── MESSAGES ENTRANTS ──────────────────────────────────────────────────────────
def process(state, msg: dict):
    """
    L API retourne:
      msg["from"] = numero expediteur
      msg["to"]   = numero recepteur (notre SIM)
      msg["content"] = texte

    On repond DEPUIS to VERS from.
    """
    mid      = msg_id(msg)
    from_num = (msg.get("from") or "").strip()
    to_num   = (msg.get("to")   or "").strip()
    content  = (msg.get("content") or "").strip()

    if not mid or not from_num or not to_num:
        return None

    # Deduplication
    seen = state.setdefault("seen", {})
    if mid in seen:
        return None
    seen[mid] = time.time()

    sims = state.get("sims", {})

    # Les deux participants doivent etre nos SIMs
    if from_num not in sims:
        return None   # expediteur externe, ignorer
    if to_num not in sims:
        return None   # destinataire inconnu, ignorer

    receiver_id = sims.get(to_num)  # SIM qui a recu = celui qui repond
    ck   = conv_key(from_num, to_num)
    conv = state.setdefault("convs", {}).get(ck)

    if conv is None:
        # Conversation inconnue → creer
        conv = {"turn": 1, "status": "active",
                "last_sender": from_num, "at": time.time()}
        state["convs"][ck] = conv

    if conv.get("status") == "done":
        return {"skip": "done", "ck": ck}

    turn = int(conv.get("turn", 1))
    if turn >= MAX_TURNS:
        conv["status"] = "done"
        print(f"  [DONE] {ck}", flush=True)
        return {"done": ck}

    if not can_send(state, receiver_id):
        return {"skip": "rate", "ck": ck}

    next_turn = turn + 1
    reply     = tpl(next_turn)

    delay = random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S)
    time.sleep(delay)

    try:
        send_sms(receiver_id, from_num, reply)
        conv["turn"]        = next_turn
        conv["last_sender"] = to_num
        conv["at"]          = time.time()
        if next_turn >= MAX_TURNS:
            conv["status"] = "done"
            print(f"  [DONE] {ck}", flush=True)
        print(f"  [REPLY] {to_num}(sim#{receiver_id}) -> {from_num} tour={next_turn}", flush=True)
        return {"replied": ck, "turn": next_turn}
    except Exception as e:
        return {"err": str(e), "ck": ck}

# ─── MAIN ───────────────────────────────────────────────────────────────────────
def run():
    if not API_TOKEN:
        raise SystemExit("SMS_GATEWAY_API_KEY manquant.")

    print("AutoChat RBSoft SMS Gateway", flush=True)
    print(f"BASE_URL = {BASE_URL}", flush=True)

    # Test connexion
    try:
        r = requests.get(f"{BASE_URL}/api/v1/devices",
                         headers=_headers(), timeout=10)
        print(f"[INIT] /api/v1/devices -> {r.status_code}", flush=True)
        if r.status_code == 401:
            raise SystemExit("401 Unauthorized — verifiez votre API token (Bearer)")
        if r.status_code != 200:
            print(f"[INIT] body: {r.text[:300]}", flush=True)
    except SystemExit:
        raise
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
            raise SystemExit(f"Seulement {len(sims)} SIM(s) valide(s), minimum 2.")
        # Purger l etat si des numeros ont change
        valid = set(sims.keys())
        old_convs = state.get("convs", {})
        state["convs"] = {ck: v for ck, v in old_convs.items()
                          if all(p in valid for p in ck.split("|", 1))}
        state["sims"]  = sims
        state["seen"]  = {}   # vider pour ne pas rater de reponses
        save_state(state)
        print(f"[INIT] {len(sims)} SIMs :", flush=True)
        for num, sid in sorted(sims.items()):
            print(f"  {num} -> sim_id={sid}", flush=True)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Erreur SIMs : {e}")

    print(f"\nDemarrage dans 3s... MAX_TURNS={MAX_TURNS} POLL={POLL_INTERVAL_S}s\n", flush=True)
    time.sleep(3)

    last_refresh = 0.0
    last_tick    = 0.0

    while True:
        try:
            now = time.time()

            # Rafraichissement periodique SIMs
            if now - last_refresh >= SIM_REFRESH_S:
                try:
                    fresh = fetch_sims()
                    if fresh:
                        valid = set(fresh.keys())
                        state["convs"] = {ck: v for ck, v in state.get("convs", {}).items()
                                          if all(p in valid for p in ck.split("|", 1))}
                        state["sims"] = fresh
                        last_refresh  = now
                        print(f"[SIMS] {len(fresh)}: {sorted(fresh.keys())}", flush=True)
                except Exception as e:
                    print(f"[WARN refresh] {e}", flush=True)

            sims = state.get("sims", {})
            if len(sims) < 2:
                print("[WARN] < 2 SIMs actifs, attente...", flush=True)
                time.sleep(15)
                continue

            # ── Messages entrants → reponse automatique ────────────────────
            try:
                msgs = sorted(
                    fetch_received(),
                    key=lambda x: int(x.get("id") or 0)
                )
                # Debug : nouveaux messages
                seen_ids = state.get("seen", {})
                new_msgs = [m for m in msgs if msg_id(m) not in seen_ids]
                if new_msgs:
                    print(f"[INBOUND] {len(new_msgs)} nouveau(x) / {len(msgs)} total", flush=True)
                    for m in new_msgs:
                        print(f"  from={m.get('from')!r} to={m.get('to')!r} "
                              f"id={m.get('id')} content={str(m.get('content',''))[:40]!r}",
                              flush=True)

                results = []
                for m in msgs:
                    r = process(state, m)
                    if r is not None:
                        results.append(r)
                if results:
                    print(f"[IN results] {results}", flush=True)
            except Exception as e:
                import traceback
                print(f"[ERR inbound] {e}", flush=True)
                traceback.print_exc()

            # ── Tick round-robin ────────────────────────────────────────────
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
