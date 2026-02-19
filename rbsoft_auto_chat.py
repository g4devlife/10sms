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
        'conversations':  {},
        'waiting_number': None,
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
# PHASE 2 : CONVERSATIONS
# =========================
def build_text(conv_id: str, turn: int) -> str:
    base = TEMPLATES[min(max(turn - 1, 0), len(TEMPLATES) - 1)]
    return f'{CONVERSATION_TAG} id={conv_id} t={turn}] {base}'

def conv_key(a: str, b: str) -> str:
    return '|'.join(sorted([a, b]))

def build_existing_pairs(state: Dict[str, Any]) -> Set[str]:
    return {conv_key(c['a_number'], c['b_number'])
            for c in state.get('conversations', {}).values()}

def start_one_conversation(state: Dict[str, Any], a_num: str, b_num: str,
                           sims_map: Dict[str, str]) -> Optional[str]:
    a_spec = sims_map.get(a_num)
    b_spec = sims_map.get(b_num)
    if not a_spec or not b_spec:
        return None

    cid = uuid.uuid4().hex[:10]
    state['conversations'][cid] = {
        'id':          cid,
        'a_number':    a_num,
        'b_number':    b_num,
        'a_spec':      a_spec,
        'b_spec':      b_spec,
        'turn':        0,
        'max_turns':   MAX_TURNS,
        'status':      'active',
        'last_sender': None,
        'created_at':  time.time(),
    }
    if can_send(state, a_spec):
        send_sms(state, a_spec, b_num, build_text(cid, 1))
        state['conversations'][cid]['turn']        = 1
        state['conversations'][cid]['last_sender'] = a_num
    return cid

def adaptive_pairing(state: Dict[str, Any], sims_map: Dict[str, str]) -> dict:
    numbers = sorted(sims_map.keys())
    pairs   = build_existing_pairs(state)
    busy    = set()
    for c in state.get('conversations', {}).values():
        if c.get('status') == 'active':
            busy.update([c['a_number'], c['b_number']])
    print(f"  [PAIR] numbers={numbers} busy={sorted(busy)} pairs={sorted(pairs)}", flush=True)

    available = [n for n in numbers if n not in busy]
    waiting   = state.get('waiting_number')
    if waiting and waiting in sims_map and waiting not in busy and waiting not in available:
        available.insert(0, waiting)
        state['waiting_number'] = None

    random.shuffle(available)
    created = 0; created_ids = []; skipped = 0; i = 0

    while i + 1 < len(available):
        a, b = available[i], available[i + 1]
        i += 2
        key = conv_key(a, b)
        if key in pairs:
            skipped += 1; continue
        cid = start_one_conversation(state, a, b, sims_map)
        if cid:
            created += 1; created_ids.append(cid); pairs.add(key)
        else:
            skipped += 1

    if i < len(available):
        state['waiting_number'] = available[i]

    return {'created': created, 'created_ids': created_ids,
            'skipped': skipped, 'waiting': state['waiting_number']}

def find_conv(state: Dict[str, Any], from_n: str, device_id: int, sim_slot: int) -> Optional[str]:
    """
    Cherche la conversation par (from_number, device_id+slot).
    Le device_id+slot nous dit QUI a reçu le message (le destinataire dans la conversation).
    """
    for cid, c in state.get('conversations', {}).items():
        # Reconstituer les specs depuis la map connue
        sims = state.get('known_sims', {})
        a_spec = sims.get(c['a_number'], '')
        b_spec = sims.get(c['b_number'], '')

        receiver_spec = f'{device_id}|{sim_slot}'

        if c['a_number'] == from_n and b_spec == receiver_spec:
            return cid
        if c['b_number'] == from_n and a_spec == receiver_spec:
            return cid
    return None

def process_inbound(state: Dict[str, Any], msg: dict) -> Optional[dict]:
    mid     = msg_id_from(msg)
    from_n  = msg.get('number', '')          # expéditeur
    content = msg.get('message', '')
    dev_id  = msg.get('deviceID')
    slot    = msg.get('simSlot')

    if not mid or not from_n:
        return None

    dedupe = state.setdefault('dedupe_msg_ids', {})
    if mid in dedupe:
        return {'ignored': 'duplicate', 'id': mid}
    dedupe[mid] = time.time()

    # Ignorer nos propres messages internes
    if DISCOVERY_TAG in content or CONVERSATION_TAG in content:
        return {'ignored': 'internal', 'id': mid}

    if dev_id is None or slot is None:
        return {'ignored': 'no_device_info', 'id': mid}

    cid = find_conv(state, from_n, int(dev_id), int(slot))
    if not cid:
        return {'ignored': 'no_match', 'from': from_n, 'id': mid}

    conv = state['conversations'][cid]
    if conv['status'] != 'active':
        return {'ignored': 'inactive', 'conv': cid}
    if int(conv['turn']) >= int(conv['max_turns']):
        conv['status'] = 'done'
        return {'stopped': 'max_turns', 'conv': cid}

    # Le répondeur = celui dont la SIM a reçu le message
    receiver_spec = f'{dev_id}|{slot}'
    sims = state.get('known_sims', {})
    # Trouver le numéro du répondeur
    responder_num = None
    for num, spec in sims.items():
        if spec == receiver_spec:
            responder_num = num
            break

    if not responder_num or not can_send(state, receiver_spec):
        return {'skipped': 'rate_limited_or_unknown', 'conv': cid}

    next_turn  = int(conv['turn']) + 1
    reply_text = build_text(cid, next_turn)

    time.sleep(random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S))
    send_sms(state, receiver_spec, from_n, reply_text)

    conv['turn']        = next_turn
    conv['last_sender'] = responder_num
    if int(conv['turn']) >= int(conv['max_turns']):
        conv['status'] = 'done'

    return {'replied': True, 'conv': cid, 'turn': conv['turn'], 'id': mid}

# =========================
# MAIN
# =========================
def run():
    if not API_KEY:
        raise SystemExit("Variable SMS_GATEWAY_API_KEY (ou RBSOFT_TOKEN) manquante.")

    print("AutoChat ExaGate — demarrage", flush=True)
    print(f"BASE_URL = {BASE_URL}", flush=True)

    # Vérification de connectivité
    try:
        r = requests.get(f"{BASE_URL}{EP_DEVICES}", headers=_headers(),
                         params=_base_params(), timeout=10)
        print(f"[INIT] Status /services/get-devices.php -> {r.status_code}", flush=True)
        body_preview = r.text[:200].replace("\n", " ")
        print(f"[INIT] Body preview: {body_preview!r}", flush=True)
    except Exception as e:
        print(f"[INIT] Erreur connexion : {e}", flush=True)
        raise SystemExit(1)

    # ── Chargement état ──────────────────────────────────────────────────────
    with _lock:
        state = load_state()

    # RESET_STATE=1 : efface les conversations et redémarre la découverte
    if os.getenv("RESET_STATE", "0") == "1":
        print("[INIT] RESET_STATE=1 : nettoyage de l'état...", flush=True)
        state["conversations"]  = {}
        state["dedupe_msg_ids"] = {}
        state["waiting_number"] = None
        state["rate"]           = {"global": [], "per_sim": {}}
        state["discovery"]      = _default_state()["discovery"]
        state["known_sims"]     = {}

    # Nettoyer les conversations "active" dont les numéros ne sont plus dans known_sims
    # (peut arriver après un redémarrage avec des SIMs changés)
    active_before = sum(1 for c in state.get("conversations", {}).values() if c.get("status") == "active")
    if active_before > 0:
        print(f"[INIT] {active_before} conversations actives chargées depuis l'état.", flush=True)

    atomic_save(state)

    # ── PHASE 1 : DÉCOUVERTE ─────────────────────────────────────────────────
    if not state['discovery']['done']:
        print('\n══ PHASE 1 : DÉCOUVERTE DES SIMs ══', flush=True)
        with _lock:
            state = load_state()
        try:
            confirmed_sims = run_discovery_phase(state)
        except Exception as e:
            print(f'[DISCOVERY] Échec : {e}', flush=True)
            raise SystemExit(1)
        with _lock:
            state = load_state()
        state['known_sims'] = confirmed_sims
        atomic_save(state)
        print(f'\n✅ {len(confirmed_sims)} SIMs confirmés :', flush=True)
        for num, spec in confirmed_sims.items():
            print(f'   {num} → {spec}', flush=True)
    else:
        confirmed_sims = {k: v for k, v in state['discovery']['confirmed_sims'].items()}
        print('[DISCOVERY] Déjà effectuée.', flush=True)
        for num, spec in confirmed_sims.items():
            print(f'   {num} → {spec}', flush=True)

    print('\nDémarrage des conversations dans 3s…', flush=True)
    time.sleep(3)

    # ── PHASE 2 : CONVERSATIONS ───────────────────────────────────────────────
    print('\n══ PHASE 2 : CONVERSATIONS ══\n', flush=True)

    while True:
        try:
            now = time.time()

            with _lock:
                state = load_state()
            last_refresh = float(state.get('meta', {}).get('last_sim_refresh', 0) or 0)

            if (now - last_refresh) >= SIM_REFRESH_INTERVAL_S:
                with _lock:
                    state = load_state()
                fresh = fetch_sims(state)
                # Conserver uniquement les SIMs connus
                sims_map = {n: s for n, s in fresh.items() if n in confirmed_sims}
                state['known_sims'] = sims_map
                state.setdefault('meta', {})['last_sim_refresh'] = now
                pairing = adaptive_pairing(state, sims_map)
                atomic_save(state)
                print(f'[SIMS] {len(sims_map)} actifs | Pairing={pairing}', flush=True)

            with _lock:
                state = load_state()
            msgs        = fetch_received_messages(state)
            msgs_sorted = sorted(msgs, key=lambda x: int(x.get('id') or x.get('ID') or 0))
            updates     = []
            for m in msgs_sorted:
                out = process_inbound(state, m)
                if out:
                    updates.append(out)
            atomic_save(state)
            if updates:
                print('Updates:', updates, flush=True)

        except Exception as e:
            print(f'Error: {repr(e)}', flush=True)

        time.sleep(POLL_INTERVAL_S)


if __name__ == '__main__':
    run()
