"""
AutoChat – RBSoft / ExaGate Edition
====================================
Adapté au vrai code source PHP de gate.exanewtech.com :

  Auth   : ?key=API_KEY  (query param, PAS de Bearer)
  Devices: GET /services/get-devices.php?key=...
  Send   : GET /services/send.php?key=...&number=...&message=...&devices=DEVICE_ID|SLOT
  Messages: GET /services/get-messages.php?key=...&status=Received

Structure SIM retournée :
  data.devices[].sims = { "0": "SIM #1 [+237612345678]", "1": "SIM #2 [...]" }
"""

import os
import re
import json
import time
import uuid
import random
import tempfile
import threading
from typing import Dict, Any, List, Optional, Set, Tuple
import requests
from requests import HTTPError

# =========================
# CONFIG
# =========================
BASE_URL = os.getenv('SMS_GATEWAY_URL') or os.getenv('RBSOFT_BASE_URL') or 'https://gate.exanewtech.com'
BASE_URL = BASE_URL.rstrip('/')

# La clé API (query param ?key=...), PAS un Bearer token
API_KEY  = os.getenv('SMS_GATEWAY_API_KEY') or os.getenv('RBSOFT_TOKEN') or ''

STATE_FILE             = os.getenv('STATE_FILE',             'rbsoft_state.json')
MAX_TURNS              = int(os.getenv('MAX_TURNS',              '10'))
POLL_INTERVAL_S        = int(os.getenv('POLL_INTERVAL_S',        '4'))
SIM_REFRESH_INTERVAL_S = int(os.getenv('SIM_REFRESH_INTERVAL_S', '30'))
REPLY_DELAY_MIN_S      = int(os.getenv('REPLY_DELAY_MIN_S',      '2'))
REPLY_DELAY_MAX_S      = int(os.getenv('REPLY_DELAY_MAX_S',      '7'))
GLOBAL_SEND_PER_MIN    = int(os.getenv('GLOBAL_SEND_PER_MIN',    '120'))
PER_SIM_SEND_PER_MIN   = int(os.getenv('PER_SIM_SEND_PER_MIN',   '30'))
DISCOVERY_WAIT_S       = int(os.getenv('DISCOVERY_WAIT_S',       '30'))
MIN_SIMS_REQUIRED      = int(os.getenv('MIN_SIMS_REQUIRED',      '2'))

# ── Vrais endpoints (découverts dans le code source PHP) ─────────────────────
EP_DEVICES  = '/services/get-devices.php'
EP_SEND     = '/services/send.php'
EP_MESSAGES = '/services/get-messages.php'   # supporte ?status=Received

DISCOVERY_TAG    = '[AUTOCHAT:REGISTER]'
CONVERSATION_TAG = '[AUTOCHAT:CONV'

TEMPLATES = [
    "Hello 👋",
    "Comment ca va aujourd'hui ?",
    "Tu fais quoi de beau en ce moment ?",
    "La journee s'est bien passee ?",
    "Tu as bien mange ? 😄",
    "Tu as des nouvelles ?",
    "Tu bosses sur quoi ces jours-ci ?",
    "Ca fait plaisir d'avoir de tes nouvelles.",
    "On se capte bientot !",
    "Bon je te laisse — prends soin de toi 🙏"
]

# =========================
# HTTP helpers
# =========================
def _base_params() -> dict:
    """Paramètres d'authentification à ajouter à chaque requête."""
    return {'key': API_KEY}

def _headers() -> dict:
    return {"Accept": "application/json"}

def _safe_json(r: requests.Response, context: str = "") -> dict:
    """Parse JSON de manière sécurisée — retourne {} si la réponse est vide ou non-JSON."""
    body = r.text.strip() if r.text else ""
    if not body:
        # Réponse vide : normal quand aucun message (ex: 204 No Content)
        return {}
    try:
        return r.json()
    except ValueError:
        # Log uniquement si ce n'est pas une réponse HTML/vide attendue
        snippet = body[:120].replace("\n", " ")
        print(f"[WARN] JSON parse fail ({context}) status={r.status_code} body={snippet!r}", flush=True)
        return {}

def api_get(path: str, params: Optional[dict] = None) -> dict:
    p = {**_base_params(), **(params or {})}
    r = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=p, timeout=30)
    r.raise_for_status()
    data = _safe_json(r, path)
    if isinstance(data, dict) and data.get("success") is False:
        err  = data.get("error", {})
        code = err.get("code", 0) if isinstance(err, dict) else 0
        msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"API error {code}: {msg}")
    return data

def api_get_raw(path: str, params: Optional[dict] = None) -> dict:
    """Retourne le dict brut sans lever d'exception sur success=False."""
    p = {**_base_params(), **(params or {})}
    r = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=p, timeout=30)
    r.raise_for_status()
    return _safe_json(r, path)

def api_post(path: str, payload: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    p = {**_base_params(), **(params or {})}
    r = requests.post(f"{BASE_URL}{path}", headers=_headers(), params=p,
                      data=payload or {}, timeout=30)
    r.raise_for_status()
    data = _safe_json(r, path)
    if isinstance(data, dict) and data.get("success") is False:
        err  = data.get("error", {})
        code = err.get("code", 0) if isinstance(err, dict) else 0
        msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"API error {code}: {msg}")
    return data

# =========================
# Parsing des SIMs
# =========================
_SIM_NUMBER_RE = re.compile(r'\[([^\]]+)\]')

def parse_sim_number(sim_str: str) -> Optional[str]:
    """
    Extrait le numéro depuis la représentation textuelle du SIM.
    Ex: "SIM #1 [+237612345678]" → "+237612345678"
    """
    m = _SIM_NUMBER_RE.search(sim_str)
    if m:
        return m.group(1).strip()
    return None

def build_device_spec(device_id: int, slot: int) -> str:
    """
    Format attendu par send.php pour cibler une SIM précise.
    Ex: "42|0"
    """
    return f'{device_id}|{slot}'

# =========================
# State JSON atomic save
# =========================
_lock = threading.Lock()

def _default_state() -> Dict[str, Any]:
    return {
        # Paires de conversation: {"numA|numB": {sender, receiver, turn, status}}
        'pairs':          {},
        # Routage des réponses: {"numB": "numA"} = quand numB écrit, répondre en tant que numA
        'reply_routing':  {},
        # Round-robin: quel SIM est l'émetteur courant
        'round_robin':    {'sender_idx': 0, 'cycle': 0},
        'known_sims':     {},          # {number: "device_id|slot"}
        'dedupe_msg_ids': {},
        'rate':           {'global': [], 'per_sim': {}},
        'meta':           {'last_sim_refresh': 0},
        'discovery': {
            'done':             False,
            'collector_number': None,
            'collector_spec':   None,  # "device_id|slot" du collecteur
            'confirmed_sims':   {},    # {number: "device_id|slot"}
            'all_sims':         {},    # {number: "device_id|slot"}
        },
    }

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return _default_state()
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        state = json.load(f)
    if 'discovery' not in state:
        state['discovery'] = _default_state()['discovery']
    return state

def atomic_save(state: Dict[str, Any]) -> None:
    # Temp file in same dir as STATE_FILE to avoid cross-device rename (Render.com)
    state_dir = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    fd, tmp = tempfile.mkstemp(prefix="rbsoft_", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        # Fallback: direct write if rename still fails
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            print(f"[WARN] atomic_save failed: {e2}", flush=True)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass

# =========================
# Rate limiting
# =========================
def _prune(ts_list: List[float], window_s: int = 60) -> List[float]:
    now = time.time()
    return [t for t in ts_list if (now - t) <= window_s]

def can_send(state: Dict[str, Any], spec: str) -> bool:
    """spec = 'device_id|slot' ou 'device_id'."""
    now  = time.time()
    rate = state.setdefault('rate', {'global': [], 'per_sim': {}})
    rate['global'] = _prune(rate.get('global', []))
    per = rate.setdefault('per_sim', {})
    per[spec] = _prune(per.get(spec, []))
    if len(rate['global']) >= GLOBAL_SEND_PER_MIN:
        return False
    if len(per[spec]) >= PER_SIM_SEND_PER_MIN:
        return False
    rate['global'].append(now)
    per[spec].append(now)
    return True

# =========================
# Fetch SIMs (API réelle)
# =========================
def fetch_sims(state: Dict[str, Any]) -> Dict[str, str]:
    """
    Retourne {phone_number: "device_id|slot"} depuis /services/get-devices.php.

    Structure de réponse :
    {
      "success": true,
      "data": {
        "devices": [
          {
            "id": 42,
            "sims": {
              "0": "SIM #1 [+237612345678]",
              "1": "SIM #2 [+237699876543]"
            }
          }
        ]
      }
    }
    """
    data     = api_get(EP_DEVICES)
    devices  = data.get('data', {}).get('devices', [])
    sims_map: Dict[str, str] = {}

    for dev in devices:
        dev_id = dev.get('id')
        sims   = dev.get('sims', {})
        if not dev_id or not sims:
            continue
        for slot_str, sim_repr in sims.items():
            number = parse_sim_number(sim_repr)
            if number:
                spec = build_device_spec(int(dev_id), int(slot_str))
                sims_map[number] = spec

    return sims_map

# =========================
# Send SMS (API réelle)
# =========================
def send_sms(state: Dict[str, Any], spec: str, to_number: str, message: str) -> None:
    """
    Envoie un SMS via GET /services/send.php?key=...&number=...&message=...&devices=DEVICE_ID|SLOT
    """
    params = {
        'number':   to_number,
        'message':  message,
        'devices':  spec,
        'type':     'sms',
        'prioritize': 1,
    }
    # Le gateway accepte GET et POST — on utilise GET pour la simplicité
    p = {**_base_params(), **params}
    r = requests.get(f'{BASE_URL}{EP_SEND}', headers=_headers(), params=p, timeout=30)
    r.raise_for_status()
    data = _safe_json(r, EP_SEND)
    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("error", {})
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"send_sms error: {msg}")
    print(f"  [SMS] {spec} -> {to_number} | {message[:40]}", flush=True)

# =========================
# Fetch messages reçus
# =========================
def fetch_received_messages(state: Dict[str, Any]) -> List[dict]:
    """
    GET /services/get-messages.php?key=...&status=Received
    Réponse: {"success": true, "data": {"messages": [{number, message, status, deviceID, simSlot, ...}]}}
    """
    data = api_get_raw(EP_MESSAGES, params={'status': 'Received'})
    if not isinstance(data, dict) or not data.get('success'):
        return []
    return data.get('data', {}).get('messages', [])

def msg_id_from(msg: dict) -> Optional[str]:
    """Retourne un ID stable pour dédupliquer (ID BDD ou hash contenu)."""
    mid = msg.get('id') or msg.get('ID')
    if mid:
        return str(mid)
    # Fallback : hash du contenu
    return f"{msg.get('number','')}-{msg.get('message','')}-{msg.get('sentDate','')}"

# =========================
# PHASE 1 : DÉCOUVERTE
# =========================
def discovery_select_collector(sims_map: Dict[str, str]) -> Tuple[str, str]:
    """Choisit aléatoirement le tél collecteur."""
    if not sims_map:
        raise RuntimeError("Aucun SIM trouve via l'API.")
    number = random.choice(sorted(sims_map.keys()))
    return number, sims_map[number]

def discovery_send_registrations(state: Dict[str, Any], sims_map: Dict[str, str]) -> None:
    """Chaque SIM (sauf le collecteur) envoie un SMS de registration."""
    disc    = state['discovery']
    col_num = disc['collector_number']

    print(f'\n[DISCOVERY] Collecteur : {col_num} (spec={disc["collector_spec"]})', flush=True)
    print(f'[DISCOVERY] Envoi registration depuis {len(sims_map)-1} SIM(s)...', flush=True)

    for number, spec in sorted(sims_map.items()):
        if number == col_num:
            continue
        msg = f'{DISCOVERY_TAG} number={number} spec={spec}'
        try:
            send_sms(state, spec, col_num, msg)
            print(f'  [REG] {number} ({spec}) → {col_num}', flush=True)
            time.sleep(random.uniform(1.0, 2.5))
        except Exception as e:
            print(f'  [REG] ERREUR {number}: {e}', flush=True)

def parse_registration(content: str) -> Optional[Tuple[str, str]]:
    """Parse un SMS de registration. Retourne (number, spec) ou None."""
    if DISCOVERY_TAG not in content:
        return None
    try:
        number = spec = None
        for part in content.split():
            if part.startswith('number='):
                number = part.split('=', 1)[1]
            elif part.startswith('spec='):
                spec = part.split('=', 1)[1]
        if number and spec:
            return number, spec
    except Exception:
        pass
    return None

def discovery_collect_registrations(state: Dict[str, Any]) -> None:
    """Poll les messages du collecteur pour confirmer chaque SIM."""
    disc      = state['discovery']
    col_num   = disc['collector_number']
    all_sims  = disc['all_sims']
    expected  = len(all_sims) - 1
    confirmed = disc['confirmed_sims']
    dedupe    = state.setdefault('dedupe_msg_ids', {})

    print(f'\n[DISCOVERY] Attente de {expected} SMS (timeout={DISCOVERY_WAIT_S}s)...', flush=True)

    deadline = time.time() + DISCOVERY_WAIT_S
    while time.time() < deadline:
        try:
            msgs = fetch_received_messages(state)
            for msg in msgs:
                mid     = msg_id_from(msg)
                content = msg.get('message', '')
                # Filtrer uniquement les messages à destination du collecteur
                # (le gateway filtre déjà par user, on vérifie le tag)
                if mid in dedupe:
                    continue
                parsed = parse_registration(content)
                if parsed:
                    num, spec = parsed
                    dedupe[mid] = time.time()
                    if num not in confirmed:
                        confirmed[num] = spec
                        print(f'  ✓ CONFIRMÉ {num} (spec={spec})', flush=True)

            atomic_save(state)
            if len(confirmed) >= expected:
                print(f'[DISCOVERY] Tous confirmés ({len(confirmed)}/{expected}).', flush=True)
                break
        except Exception as e:
            print(f'[DISCOVERY] Erreur polling : {e}', flush=True)

        time.sleep(POLL_INTERVAL_S)

    # Fallback : ajouter les non-confirmés depuis l'API
    missing = {n: s for n, s in all_sims.items()
               if n != col_num and n not in confirmed}
    if missing:
        print(f'[DISCOVERY] Fallback API pour {len(missing)} SIM(s) : {list(missing.keys())}', flush=True)
        confirmed.update(missing)

    # Inclure le collecteur lui-même
    confirmed[col_num] = all_sims[col_num]
    disc['confirmed_sims'] = confirmed
    disc['done'] = True
    print(f'[DISCOVERY] Terminée. SIMs : {list(confirmed.keys())}', flush=True)

def run_discovery_phase(state: Dict[str, Any]) -> Dict[str, str]:
    """Orchestre la phase de découverte complète."""
    disc     = state['discovery']
    sims_map = fetch_sims(state)

    if len(sims_map) < MIN_SIMS_REQUIRED:
        raise RuntimeError(
            f'Seulement {len(sims_map)} SIM(s) trouvé(s), minimum requis : {MIN_SIMS_REQUIRED}')

    disc['all_sims'] = sims_map
    col_num, col_spec = discovery_select_collector(sims_map)
    disc['collector_number'] = col_num
    disc['collector_spec']   = col_spec
    atomic_save(state)

    discovery_send_registrations(state, sims_map)
    atomic_save(state)

    discovery_collect_registrations(state)
    atomic_save(state)

    return dict(disc['confirmed_sims'])

# =========================
# PHASE 2 : ROUND-ROBIN BROADCAST
# =========================
# Logique :
#   - SIM[0] envoie à tous les autres (SIM[1], SIM[2], ...)
#   - Chaque destinataire répond automatiquement
#   - Quand toutes les paires de SIM[0] sont terminées → SIM[1] envoie à tous
#   - Cycle infini (ou arrêt après MAX_TURNS échanges par paire)
#
# Messages : UNIQUEMENT les templates, aucun tag interne

def pair_key(a: str, b: str) -> str:
    """Clé de paire directionnelle: émetteur|destinataire."""
    return f"{a}|{b}"

def pick_template(turn: int) -> str:
    """Retourne le template correspondant au tour (cyclique)."""
    return TEMPLATES[(turn - 1) % len(TEMPLATES)]

def get_sender_number(state: Dict[str, Any], sims_list: List[str]) -> str:
    """Retourne le numéro de l'émetteur courant selon le round-robin."""
    rr  = state.setdefault("round_robin", {"sender_idx": 0, "cycle": 0})
    idx = rr.get("sender_idx", 0) % len(sims_list)
    return sims_list[idx]

def all_pairs_done(state: Dict[str, Any], sender: str, sims_list: List[str]) -> bool:
    """Vérifie si toutes les paires de l'émetteur courant sont terminées."""
    targets = [n for n in sims_list if n != sender]
    if not targets:
        return True
    pairs = state.get("pairs", {})
    for t in targets:
        pk = pair_key(sender, t)
        p  = pairs.get(pk, {})
        if p.get("status") != "done":
            return False
    return True

def advance_round_robin(state: Dict[str, Any], sims_list: List[str]) -> str:
    """Passe au prochain émetteur, retourne le nouveau numéro."""
    rr  = state.setdefault("round_robin", {"sender_idx": 0, "cycle": 0})
    rr["sender_idx"] = (rr.get("sender_idx", 0) + 1) % len(sims_list)
    if rr["sender_idx"] == 0:
        rr["cycle"] = rr.get("cycle", 0) + 1
        # Réinitialiser toutes les paires pour le nouveau cycle
        print(f"[RR] Cycle {rr['cycle']} — réinitialisation des paires", flush=True)
        state["pairs"] = {}
        state["reply_routing"] = {}
    new_sender = sims_list[rr["sender_idx"]]
    print(f"[RR] Nouvel émetteur → {new_sender} (idx={rr['sender_idx']})", flush=True)
    return new_sender

def tick_round_robin(state: Dict[str, Any], sims_map: Dict[str, str]) -> dict:
    """
    Lance les envois pour l'émetteur courant vers tous les autres.
    Avance au suivant si toutes ses paires sont terminées.
    """
    if len(sims_map) < 2:
        return {"skipped": "not_enough_sims"}

    sims_list = sorted(sims_map.keys())
    sender    = get_sender_number(state, sims_list)
    sender_spec = sims_map[sender]
    pairs     = state.setdefault("pairs", {})
    routing   = state.setdefault("reply_routing", {})

    # Vérifier si l'émetteur courant a fini toutes ses paires
    if all_pairs_done(state, sender, sims_list):
        print(f"[RR] {sender} a terminé toutes ses paires.", flush=True)
        sender = advance_round_robin(state, sims_list)
        sender_spec = sims_map[sender]

    targets  = [n for n in sims_list if n != sender]
    sent     = 0
    skipped  = 0

    for target in targets:
        pk = pair_key(sender, target)
        p  = pairs.get(pk)

        if p is None:
            # Nouvelle paire : envoi du 1er message
            if not can_send(state, sender_spec):
                skipped += 1
                continue
            msg_text = pick_template(1)
            try:
                send_sms(state, sender_spec, target, msg_text)
                pairs[pk] = {
                    "sender":   sender,
                    "receiver": target,
                    "turn":     1,
                    "status":   "active",
                    "last_sent_at": time.time(),
                }
                # Enregistrer le routage : quand target répond → répondre via sender
                routing[target] = sender
                sent += 1
                time.sleep(random.uniform(1.0, 3.0))
            except Exception as e:
                print(f"  [ERR] send {sender}→{target}: {e}", flush=True)
                skipped += 1

        elif p.get("status") == "done":
            skipped += 1  # déjà terminée

        # status == "active" → en attente de réponse, ne rien faire

    return {"sender": sender, "sent": sent, "skipped": skipped,
            "targets": targets, "active_pairs": sum(
                1 for p in pairs.values() if p.get("status") == "active")}

def process_inbound(state: Dict[str, Any], msg: dict) -> Optional[dict]:
    """
    Traite un message reçu.
    Matching simplifié : basé sur reply_routing[from_number] → pas besoin de deviceID/simSlot.
    """
    mid     = msg_id_from(msg)
    from_n  = (msg.get("number") or "").strip()
    content = (msg.get("message") or "").strip()

    if not mid or not from_n:
        return None

    dedupe = state.setdefault("dedupe_msg_ids", {})
    if mid in dedupe:
        return {"ignored": "duplicate", "id": mid}
    dedupe[mid] = time.time()

    # Ignorer les SMS de découverte
    if DISCOVERY_TAG in content:
        return {"ignored": "discovery_msg", "id": mid}

    sims_map = state.get("known_sims", {})

    # from_n doit être un de nos SIMs connus
    if from_n not in sims_map:
        return {"ignored": "unknown_sender", "from": from_n, "id": mid}

    # Trouver l'émetteur original via le routing
    routing    = state.get("reply_routing", {})
    sender_num = routing.get(from_n)  # numA (celui qui avait envoyé en premier)

    if not sender_num:
        return {"ignored": "no_routing", "from": from_n, "id": mid}

    sender_spec = sims_map.get(sender_num)
    if not sender_spec:
        return {"ignored": "sender_spec_missing", "from": from_n, "id": mid}

    # Trouver la paire
    pk   = pair_key(sender_num, from_n)
    pair = state.get("pairs", {}).get(pk)

    if not pair:
        return {"ignored": "no_pair", "pk": pk, "id": mid}

    if pair.get("status") == "done":
        return {"ignored": "pair_done", "pk": pk, "id": mid}

    turn = int(pair.get("turn", 1))
    if turn >= MAX_TURNS:
        pair["status"] = "done"
        return {"done": True, "pk": pk, "turn": turn}

    # Rate limit
    if not can_send(state, sender_spec):
        return {"skipped": "rate_limited", "pk": pk}

    next_turn = turn + 1
    reply_txt = pick_template(next_turn)

    time.sleep(random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S))
    try:
        send_sms(state, sender_spec, from_n, reply_txt)
        pair["turn"]         = next_turn
        pair["last_sent_at"] = time.time()
        if next_turn >= MAX_TURNS:
            pair["status"] = "done"
            print(f"  [DONE] Paire {pk} terminée ({next_turn} tours)", flush=True)
        return {"replied": True, "pk": pk, "turn": next_turn, "id": mid}
    except Exception as e:
        return {"error": str(e), "pk": pk, "id": mid}

# =========================
# MAIN
# =========================
def run():
    if not API_KEY:
        raise SystemExit("Variable SMS_GATEWAY_API_KEY (ou RBSOFT_TOKEN) manquante.")

    print("AutoChat ExaGate — Round-Robin Broadcast", flush=True)
    print(f"BASE_URL = {BASE_URL}", flush=True)

    # Vérification de connectivité
    try:
        r = requests.get(f"{BASE_URL}{EP_DEVICES}", headers=_headers(),
                         params=_base_params(), timeout=10)
        print(f"[INIT] /services/get-devices.php -> {r.status_code}", flush=True)
        body_preview = r.text[:200].replace("\n", " ")
        print(f"[INIT] Body: {body_preview!r}", flush=True)
    except Exception as e:
        print(f"[INIT] Erreur connexion : {e}", flush=True)
        raise SystemExit(1)

    # ── Chargement état ──────────────────────────────────────────────────────
    with _lock:
        state = load_state()

    # RESET_STATE=1 : repart de zéro
    if os.getenv("RESET_STATE", "0") == "1":
        print("[INIT] RESET_STATE=1 — nettoyage complet", flush=True)
        state = _default_state()

    atomic_save(state)

    # ── PHASE 1 : DÉCOUVERTE ─────────────────────────────────────────────────
    if not state["discovery"]["done"]:
        print("\n══ PHASE 1 : DÉCOUVERTE DES SIMs ══", flush=True)
        with _lock:
            state = load_state()
        try:
            confirmed_sims = run_discovery_phase(state)
        except Exception as e:
            print(f"[DISCOVERY] Échec : {e}", flush=True)
            raise SystemExit(1)
        with _lock:
            state = load_state()
        state["known_sims"] = confirmed_sims
        atomic_save(state)
        print(f"\n✅ {len(confirmed_sims)} SIMs confirmés :", flush=True)
        for num, spec in confirmed_sims.items():
            print(f"   {num} -> {spec}", flush=True)
    else:
        confirmed_sims = {k: v for k, v in state["discovery"]["confirmed_sims"].items()}
        print("[DISCOVERY] Déjà effectuée.", flush=True)
        for num, spec in confirmed_sims.items():
            print(f"   {num} -> {spec}", flush=True)

    if len(confirmed_sims) < 2:
        raise SystemExit(f"Seulement {len(confirmed_sims)} SIM(s) — minimum 2 requis.")

    print("\nDémarrage dans 3s…", flush=True)
    time.sleep(3)

    # ── PHASE 2 : ROUND-ROBIN BROADCAST ──────────────────────────────────────
    print("\n══ PHASE 2 : ROUND-ROBIN BROADCAST ══\n", flush=True)

    last_sim_refresh = 0.0
    rr_tick_interval = int(os.getenv("RR_TICK_INTERVAL_S", "15"))  # fréquence des envois initiaux

    while True:
        try:
            now = time.time()

            # Rafraîchissement périodique des SIMs
            if (now - last_sim_refresh) >= SIM_REFRESH_INTERVAL_S:
                with _lock:
                    state = load_state()
                fresh    = fetch_sims(state)
                sims_map = {n: s for n, s in fresh.items() if n in confirmed_sims}
                state["known_sims"] = sims_map
                state.setdefault("meta", {})["last_sim_refresh"] = now
                atomic_save(state)
                last_sim_refresh = now
                print(f"[SIMS] {len(sims_map)} actifs: {sorted(sims_map.keys())}", flush=True)
            else:
                with _lock:
                    state = load_state()
                sims_map = state.get("known_sims", {}) or confirmed_sims

            if not sims_map:
                print("[WARN] Aucun SIM actif, attente...", flush=True)
                time.sleep(POLL_INTERVAL_S * 2)
                continue

            # ── Traitement des messages entrants ──────────────────────────
            msgs = fetch_received_messages(state)
            msgs_sorted = sorted(msgs, key=lambda x: int(x.get("id") or x.get("ID") or 0))
            updates = []
            for m in msgs_sorted:
                out = process_inbound(state, m)
                if out:
                    updates.append(out)
            if updates:
                print(f"[INBOUND] {updates}", flush=True)

            # ── Tick round-robin (lancer les envois initiaux) ─────────────
            rr_result = tick_round_robin(state, sims_map)
            if rr_result.get("sent", 0) > 0 or rr_result.get("active_pairs", 0) > 0:
                print(f"[RR] {rr_result}", flush=True)

            atomic_save(state)

        except Exception as e:
            print(f"Error: {repr(e)}", flush=True)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    run()
