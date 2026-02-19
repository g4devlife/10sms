import os
import json
import time
import uuid
import random
import tempfile
import threading
from typing import Dict, Any, List, Optional, Set
import requests
from requests import HTTPError

# =========================
# CONFIG (ENV FIRST)
# =========================
BASE_URL = os.getenv('SMS_GATEWAY_URL') or os.getenv('RBSOFT_BASE_URL') or 'https://gate.exanewtech.com'
BASE_URL = BASE_URL.rstrip('/')
TOKEN    = os.getenv('SMS_GATEWAY_API_KEY') or os.getenv('RBSOFT_TOKEN') or ''

STATE_FILE              = os.getenv('STATE_FILE',              'rbsoft_state.json')
MAX_TURNS               = int(os.getenv('MAX_TURNS',               '10'))
POLL_INTERVAL_S         = int(os.getenv('POLL_INTERVAL_S',         '4'))
SIM_REFRESH_INTERVAL_S  = int(os.getenv('SIM_REFRESH_INTERVAL_S',  '30'))
REPLY_DELAY_MIN_S       = int(os.getenv('REPLY_DELAY_MIN_S',       '2'))
REPLY_DELAY_MAX_S       = int(os.getenv('REPLY_DELAY_MAX_S',       '7'))
GLOBAL_SEND_PER_MIN     = int(os.getenv('GLOBAL_SEND_PER_MIN',     '120'))
PER_SIM_SEND_PER_MIN    = int(os.getenv('PER_SIM_SEND_PER_MIN',    '30'))

# Délai d'attente pour que le collecteur reçoive tous les SMS d'enregistrement
DISCOVERY_WAIT_S        = int(os.getenv('DISCOVERY_WAIT_S',        '30'))
# Nombre minimum de SIMs attendues avant de lancer les conversations
MIN_SIMS_REQUIRED       = int(os.getenv('MIN_SIMS_REQUIRED',       '2'))

DEVICES_ENDPOINTS = [
    '/api/v1/devices', '/api/devices',
    '/api/v1/device',  '/api/device',
]
MESSAGES_LIST_ENDPOINTS = [
    '/api/v1/messages', '/api/messages',
]
MESSAGES_SEND_ENDPOINTS = [
    '/api/v1/messages/send', '/api/messages/send',
]

TEMPLATES = [
    'Hello 👋',
    "Comment ça va aujourd'hui ?",
    'Tu fais quoi de beau en ce moment ?',
    'La journée s'est bien passée ?',
    'Tu as bien mangé ? 😄',
    'Tu as des nouvelles ?',
    'Tu bosses sur quoi ces jours-ci ?',
    'Ça fait plaisir d'avoir de tes nouvelles.',
    'On se capte bientôt !',
    'Bon je te laisse — prends soin de toi 🙏'
]

# Marqueur interne pour les SMS de découverte (jamais traités comme de vrais échanges)
DISCOVERY_TAG = '[AUTOCHAT:REGISTER]'
CONVERSATION_TAG = '[TEST conv='

# =========================
# HTTP helpers
# =========================
def _headers() -> Dict[str, str]:
    return {
        'Authorization': f'Bearer {TOKEN}',
        'Content-Type':  'application/json',
        'Accept':        'application/json',
    }

def api_get(path: str, params: Optional[dict] = None) -> dict:
    r = requests.get(f'{BASE_URL}{path}', headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def api_post(path: str, payload: dict) -> dict:
    r = requests.post(f'{BASE_URL}{path}', headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}

def api_get_any(paths: List[str], params: Optional[dict] = None) -> dict:
    last_err: Optional[Exception] = None
    for p in paths:
        try:
            return api_get(p, params=params)
        except HTTPError as e:
            last_err = e
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError('No endpoints tried')

def api_post_any(paths: List[str], payload: dict) -> dict:
    last_err: Optional[Exception] = None
    for p in paths:
        try:
            return api_post(p, payload)
        except HTTPError as e:
            last_err = e
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError('No endpoints tried')

# =========================
# State JSON atomic save
# =========================
_lock = threading.Lock()

def _default_state() -> Dict[str, Any]:
    return {
        'conversations':    {},
        'waiting_number':   None,
        'known_sims':       {},
        'dedupe_msg_ids':   {},
        'rate':             {'global': [], 'per_sim': {}},
        'meta': {
            'last_sim_refresh': 0,
            'detected': {
                'devices':       None,
                'messages_list': None,
                'messages_send': None,
            },
        },
        # Phase de découverte
        'discovery': {
            'done':             False,    # True quand la phase est terminée
            'collector_number': None,     # numéro du tél collecteur
            'collector_sim_id': None,     # sim_id du collecteur
            'confirmed_sims':   {},       # {number: sim_id} — confirmés par SMS reçu
            'all_sims':         {},       # {number: sim_id} — tous les SIMs API
        },
    }

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return _default_state()
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        state = json.load(f)
    # Migration : ajouter la clé discovery si absente
    if 'discovery' not in state:
        state['discovery'] = _default_state()['discovery']
    return state

def atomic_save(state: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix='rbsoft_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

# =========================
# Rate limiting
# =========================
def _prune(ts_list: List[float], window_s: int = 60) -> List[float]:
    now = time.time()
    return [t for t in ts_list if (now - t) <= window_s]

def can_send(state: Dict[str, Any], sim_id: int) -> bool:
    now  = time.time()
    rate = state.setdefault('rate', {'global': [], 'per_sim': {}})
    rate['global'] = _prune(rate.get('global', []))
    per = rate.setdefault('per_sim', {})
    key = str(sim_id)
    per[key] = _prune(per.get(key, []))
    if len(rate['global']) >= GLOBAL_SEND_PER_MIN:
        return False
    if len(per[key]) >= PER_SIM_SEND_PER_MIN:
        return False
    rate['global'].append(now)
    per[key].append(now)
    return True

# =========================
# Endpoint detection
# =========================
def detect_endpoints(state: Dict[str, Any]) -> None:
    detected = state.setdefault('meta', {}).setdefault('detected', {})
    if detected.get('devices') and detected.get('messages_list') and detected.get('messages_send'):
        return
    for p in DEVICES_ENDPOINTS:
        try:
            api_get(p); detected['devices'] = p; break
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
    for p in MESSAGES_LIST_ENDPOINTS:
        try:
            api_get(p, params={'type': 'SMS'}); detected['messages_list'] = p; break
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue
            raise
    if not detected.get('messages_send'):
        detected['messages_send'] = MESSAGES_SEND_ENDPOINTS[0]

# =========================
# SIM discovery (API)
# =========================
def fetch_sims(state: Dict[str, Any]) -> Dict[str, int]:
    """Retourne {number: sim_id} depuis l'API des devices."""
    detect_endpoints(state)
    ep   = state['meta']['detected'].get('devices') or '/api/v1/devices'
    data = api_get(ep)
    sims_map: Dict[str, int] = {}
    items = data.get('data', []) if isinstance(data, dict) else []
    for dev in items:
        for sim in dev.get('sims', []):
            number = sim.get('number')
            sim_id = sim.get('id')
            if number and sim_id:
                sims_map[number] = int(sim_id)
    return sims_map

# =========================
# SMS send (bare)
# =========================
def send_sms(state: Dict[str, Any], sim_id: int, to_number: str, message: str) -> None:
    detect_endpoints(state)
    send_ep = state['meta']['detected'].get('messages_send') or '/api/v1/messages/send'
    payload = {
        'sims':            [sim_id],
        'mobile_numbers':  [to_number],
        'type':            'SMS',
        'message':         message,
        'delivery_report': False,
        'prioritize':      True,
        'name':            f'AutoChat-{uuid.uuid4().hex[:6]}',
    }
    try:
        api_post(send_ep, payload)
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            api_post_any(MESSAGES_SEND_ENDPOINTS, payload)
            state['meta']['detected']['messages_send'] = MESSAGES_SEND_ENDPOINTS[-1]
        else:
            raise

# =========================
# Fetch inbound messages
# =========================
def fetch_received_messages(state: Dict[str, Any]) -> List[dict]:
    detect_endpoints(state)
    list_ep = state['meta']['detected'].get('messages_list') or '/api/v1/messages'
    data    = api_get(list_ep, params={'type': 'SMS', 'statuses[0]': 'Received'})
    return data.get('data', []) if isinstance(data, dict) else []

# =========================
# ██  PHASE 1 : DÉCOUVERTE ██
# =========================
def discovery_select_collector(sims_map: Dict[str, int]) -> tuple:
    """
    Choisit aléatoirement le tél collecteur parmi tous les SIMs disponibles.
    Retourne (collector_number, collector_sim_id).
    """
    if not sims_map:
        raise RuntimeError('Aucun SIM trouvé via l'API.')
    number = random.choice(sorted(sims_map.keys()))
    return number, sims_map[number]

def discovery_send_registrations(state: Dict[str, Any], sims_map: Dict[str, int]) -> None:
    """
    Chaque SIM (sauf le collecteur) envoie un SMS de registration au collecteur.
    Format : [AUTOCHAT:REGISTER] number=<mon_numero> sim_id=<mon_sim_id>
    """
    disc     = state['discovery']
    col_num  = disc['collector_number']
    col_sim  = disc['collector_sim_id']

    print(f'\n[DISCOVERY] Collecteur choisi : {col_num} (sim_id={col_sim})', flush=True)
    print(f'[DISCOVERY] Envoi des SMS de registration depuis {len(sims_map)-1} SIM(s)...', flush=True)

    for number, sim_id in sorted(sims_map.items()):
        if number == col_num:
            continue
        msg = f'{DISCOVERY_TAG} number={number} sim_id={sim_id}'
        try:
            send_sms(state, sim_id, col_num, msg)
            print(f'  [REG] {number} (sim={sim_id}) → {col_num}', flush=True)
            time.sleep(random.uniform(1.0, 2.5))   # petit délai anti-spam
        except Exception as e:
            print(f'  [REG] ERREUR {number}: {e}', flush=True)

def discovery_parse_registration(content: str) -> Optional[tuple]:
    """
    Parse un SMS de registration.
    Retourne (number, sim_id) ou None si ce n'est pas un message de registration.
    """
    if DISCOVERY_TAG not in content:
        return None
    try:
        parts = content.split()
        number = None
        sim_id = None
        for p in parts:
            if p.startswith('number='):
                number = p.split('=', 1)[1]
            elif p.startswith('sim_id='):
                sim_id = int(p.split('=', 1)[1])
        if number and sim_id:
            return number, sim_id
    except Exception:
        pass
    return None

def discovery_collect_registrations(state: Dict[str, Any], wait_s: int = DISCOVERY_WAIT_S) -> None:
    """
    Attend `wait_s` secondes en polllant les messages reçus par le collecteur.
    Remplit discovery['confirmed_sims'] au fur et à mesure.
    """
    disc       = state['discovery']
    col_num    = disc['collector_number']
    all_sims   = disc['all_sims']
    expected   = len(all_sims) - 1   # tous sauf le collecteur
    confirmed  = disc['confirmed_sims']
    dedupe     = state.setdefault('dedupe_msg_ids', {})

    print(f'\n[DISCOVERY] Attente de {expected} SMS de registration (timeout={wait_s}s)...', flush=True)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            msgs = fetch_received_messages(state)
            for msg in msgs:
                msg_id  = str(msg.get('id', ''))
                to_n    = msg.get('to', '')
                content = msg.get('content', '')

                if to_n != col_num:
                    continue
                if msg_id in dedupe:
                    continue

                parsed = discovery_parse_registration(content)
                if parsed:
                    num, sid = parsed
                    dedupe[msg_id] = time.time()
                    if num not in confirmed:
                        confirmed[num] = sid
                        print(f'  [CONFIRMED] {num} (sim_id={sid})', flush=True)

            atomic_save(state)

            if len(confirmed) >= expected:
                print(f'[DISCOVERY] Tous les SIMs confirmés ({len(confirmed)}/{expected}).', flush=True)
                break

        except Exception as e:
            print(f'[DISCOVERY] Erreur polling : {e}', flush=True)

        time.sleep(POLL_INTERVAL_S)

    # Fallback : on ajoute les SIMs non confirmés depuis l'API pour ne pas les perdre
    missing = {n: sid for n, sid in all_sims.items()
               if n != col_num and n not in confirmed}
    if missing:
        print(f'[DISCOVERY] {len(missing)} SIM(s) non confirmés, ajoutés depuis l'API : {list(missing.keys())}', flush=True)
        confirmed.update(missing)

    # Inclure le collecteur lui-même dans les SIMs utilisables
    confirmed[col_num] = all_sims[col_num]
    disc['confirmed_sims'] = confirmed
    disc['done'] = True
    print(f'[DISCOVERY] Phase terminée. SIMs confirmés : {list(confirmed.keys())}', flush=True)

def run_discovery_phase(state: Dict[str, Any]) -> Dict[str, int]:
    """
    Orchestre toute la phase de découverte.
    Retourne le sims_map confirmé {number: sim_id}.
    """
    disc = state['discovery']

    # Récupérer tous les SIMs via API
    sims_map = fetch_sims(state)
    if len(sims_map) < MIN_SIMS_REQUIRED:
        raise RuntimeError(f'Seulement {len(sims_map)} SIM(s) trouvé(s), minimum requis : {MIN_SIMS_REQUIRED}')

    disc['all_sims'] = {k: int(v) for k, v in sims_map.items()}

    # Choisir le collecteur
    col_num, col_sim = discovery_select_collector(sims_map)
    disc['collector_number'] = col_num
    disc['collector_sim_id'] = int(col_sim)
    atomic_save(state)

    # Tous les autres envoient leur SMS de registration
    discovery_send_registrations(state, sims_map)
    atomic_save(state)

    # Collecter les confirmations
    discovery_collect_registrations(state)
    atomic_save(state)

    return {k: int(v) for k, v in disc['confirmed_sims'].items()}

# =========================
# ██  PHASE 2 : CONVERSATIONS ██
# =========================
def build_text(conv_id: str, turn: int) -> str:
    base = TEMPLATES[min(max(turn - 1, 0), len(TEMPLATES) - 1)]
    return f'{CONVERSATION_TAG}{conv_id} turn={turn}] {base}'

def conv_key(a: str, b: str) -> str:
    return '|'.join(sorted([a, b]))

def build_existing_pairs_index(state: Dict[str, Any]) -> Set[str]:
    idx = set()
    for c in state.get('conversations', {}).values():
        idx.add(conv_key(c['a_number'], c['b_number']))
    return idx

def start_one_conversation(state: Dict[str, Any], a_num: str, b_num: str, sims_map: Dict[str, int]) -> Optional[str]:
    a_sim = sims_map.get(a_num)
    b_sim = sims_map.get(b_num)
    if not a_sim or not b_sim:
        return None
    cid = uuid.uuid4().hex[:10]
    state['conversations'][cid] = {
        'id':          cid,
        'a_number':    a_num,
        'b_number':    b_num,
        'a_sim':       a_sim,
        'b_sim':       b_sim,
        'turn':        0,
        'max_turns':   MAX_TURNS,
        'status':      'active',
        'last_sender': None,
        'created_at':  time.time(),
    }
    if can_send(state, a_sim):
        send_sms(state, a_sim, b_num, build_text(cid, 1))
        state['conversations'][cid]['turn']        = 1
        state['conversations'][cid]['last_sender'] = a_num
    return cid

def adaptive_pairing(state: Dict[str, Any], sims_map: Dict[str, int]) -> Dict[str, Any]:
    numbers        = sorted(list(sims_map.keys()))
    existing_pairs = build_existing_pairs_index(state)
    busy           = set()

    for c in state.get('conversations', {}).values():
        if c.get('status') == 'active':
            busy.add(c['a_number'])
            busy.add(c['b_number'])

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
        if key in existing_pairs:
            skipped += 1; continue
        cid = start_one_conversation(state, a, b, sims_map)
        if cid:
            created += 1; created_ids.append(cid); existing_pairs.add(key)
        else:
            skipped += 1

    if i < len(available):
        state['waiting_number'] = available[i]

    return {'created': created, 'created_ids': created_ids, 'skipped': skipped, 'waiting_number': state['waiting_number']}

def my_sim(conv: dict, my_number: str) -> int:
    return int(conv['a_sim']) if my_number == conv['a_number'] else int(conv['b_sim'])

def find_conv_id(state: Dict[str, Any], a: str, b: str) -> Optional[str]:
    for cid, c in state.get('conversations', {}).items():
        x, y = c['a_number'], c['b_number']
        if (a == x and b == y) or (a == y and b == x):
            return cid
    return None

def process_inbound(state: Dict[str, Any], msg: dict) -> Optional[dict]:
    msg_id  = int(msg.get('id') or 0)
    from_n  = msg.get('from') or ''
    to_n    = msg.get('to')   or ''
    content = msg.get('content') or ''

    if not msg_id or not from_n or not to_n:
        return None

    dedupe = state.setdefault('dedupe_msg_ids', {})
    if str(msg_id) in dedupe:
        return {'ignored': 'duplicate', 'id': msg_id}
    dedupe[str(msg_id)] = time.time()

    # Ignorer les SMS de découverte et les SMS propres au script
    if DISCOVERY_TAG in content or CONVERSATION_TAG in content:
        return {'ignored': 'internal_tag', 'id': msg_id}

    cid = find_conv_id(state, from_n, to_n)
    if not cid:
        return {'ignored': 'no_match', 'id': msg_id}

    conv = state['conversations'][cid]
    if conv['status'] != 'active':
        return {'ignored': 'inactive', 'id': msg_id, 'conv': cid}
    if int(conv['turn']) >= int(conv['max_turns']):
        conv['status'] = 'done'
        return {'stopped': 'max_turns', 'conv': cid}

    responder     = to_n
    responder_sim = my_sim(conv, responder)

    if not can_send(state, responder_sim):
        return {'skipped': 'rate_limited', 'conv': cid, 'sim': responder_sim}

    next_turn  = int(conv['turn']) + 1
    reply_text = build_text(cid, next_turn)
    time.sleep(random.randint(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S))
    send_sms(state, responder_sim, from_n, reply_text)

    conv['turn']        = next_turn
    conv['last_sender'] = responder
    if int(conv['turn']) >= int(conv['max_turns']):
        conv['status'] = 'done'

    return {'replied': True, 'conv': cid, 'turn': conv['turn'], 'msg_id': msg_id}

# =========================
# MAIN
# =========================
def run():
    if not TOKEN:
        raise SystemExit('Missing SMS_GATEWAY_API_KEY (or RBSOFT_TOKEN) env variable.')

    print('AutoChat (Discovery + Adaptive) started.', flush=True)
    print(f'BASE_URL={BASE_URL}', flush=True)

    # ── Chargement ou init de l'état ──────────────────────────────────────────
    with _lock:
        state = load_state()
        detect_endpoints(state)
        atomic_save(state)

    # ── PHASE 1 : DÉCOUVERTE ─────────────────────────────────────────────────
    if not state['discovery']['done']:
        print('\n══ PHASE 1 : DÉCOUVERTE DES SIMs ══', flush=True)
        with _lock:
            state = load_state()
        try:
            confirmed_sims = run_discovery_phase(state)
        except Exception as e:
            print(f'[DISCOVERY] Échec critique : {e}')
            raise SystemExit(1)
        with _lock:
            state = load_state()
        state['known_sims'] = confirmed_sims
        atomic_save(state)
        print(f'\n[DISCOVERY] ✓ {len(confirmed_sims)} SIMs prêts pour les conversations.', flush=True)
        print('Récapitulatif :', json.dumps(confirmed_sims, indent=2), flush=True)
    else:
        print('[DISCOVERY] Phase déjà effectuée, chargement des SIMs confirmés.', flush=True)
        confirmed_sims = {k: int(v) for k, v in state['discovery']['confirmed_sims'].items()}
        print(f'SIMs : {list(confirmed_sims.keys())}', flush=True)

    # Pause de courtoisie avant de démarrer les échanges
    print('\nDémarrage des conversations dans 3s...', flush=True)
    time.sleep(3)

    # ── PHASE 2 : CONVERSATIONS ───────────────────────────────────────────────
    print('\n══ PHASE 2 : CONVERSATIONS ══\n', flush=True)

    while True:
        try:
            now = time.time()

            # Rafraîchissement périodique des SIMs
            with _lock:
                state = load_state()
            last_refresh = float(state.get('meta', {}).get('last_sim_refresh', 0) or 0)

            if (now - last_refresh) >= SIM_REFRESH_INTERVAL_S:
                with _lock:
                    state = load_state()
                fresh = fetch_sims(state)
                # On conserve uniquement les SIMs confirmés à la découverte
                sims_map = {n: sid for n, sid in fresh.items() if n in confirmed_sims}
                state['known_sims'] = {k: int(v) for k, v in sims_map.items()}
                state.setdefault('meta', {})['last_sim_refresh'] = now
                pairing = adaptive_pairing(state, sims_map)
                atomic_save(state)
                print(f'SIM COUNT={len(sims_map)} Pairing={pairing}', flush=True)

            # Traitement des messages entrants
            with _lock:
                state = load_state()
            msgs         = fetch_received_messages(state)
            msgs_sorted  = sorted(msgs, key=lambda x: int(x.get('id') or 0))
            updates      = []
            for m in msgs_sorted:
                out = process_inbound(state, m)
                if out:
                    updates.append(out)
            atomic_save(state)
            if updates:
                print('Updates:', updates, flush=True)

        except Exception as e:
            print('Error:', repr(e), flush=True)

        time.sleep(POLL_INTERVAL_S)

if __name__ == '__main__':
    run()
