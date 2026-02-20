<?php
/**
 * AutoChat ExaGate — Round-Robin Full Auto
 * =========================================
 * Pas de polling inbound. La conversation complete (10 tours)
 * s'execute automatiquement avec delais humains.
 *
 * Logique :
 *   SIM A envoie a SIM B  tour 1 : "Hello !"
 *   SIM B repond a SIM A  tour 2 : "Ca va ?"
 *   SIM A repond a SIM B  tour 3 : "Tu fais quoi ?"
 *   ...jusqu'a MAX_TURNS
 *   Puis SIM suivant fait pareil avec tous, etc.
 */

define('BASE_URL',          rtrim(getenv('SMS_GATEWAY_URL')   ?: 'https://gate.exanewtech.com', '/'));
define('API_KEY',           getenv('SMS_GATEWAY_API_KEY')     ?: '');
define('STATE_FILE',        getenv('STATE_FILE')              ?: __DIR__ . '/state.json');
define('MAX_TURNS',         (int)(getenv('MAX_TURNS')         ?: 20)); // 20 = 10 msgs par tel
define('REPLY_DELAY_MIN_S', (int)(getenv('REPLY_DELAY_MIN_S') ?: 20));
define('REPLY_DELAY_MAX_S', (int)(getenv('REPLY_DELAY_MAX_S') ?: 40));
define('PAIR_DELAY_MIN_S',  (int)(getenv('PAIR_DELAY_MIN_S')  ?: 5));
define('PAIR_DELAY_MAX_S',  (int)(getenv('PAIR_DELAY_MAX_S')  ?: 10));
define('RR_PAUSE_S',        (int)(getenv('RR_PAUSE_S')        ?: 60));

// 20 templates = 10 messages par telephone (tour impair = A, tour pair = B)
const TEMPLATES = [
    1  => "Hello !",
    2  => "Ca va de ton cote ?",
    3  => "Tu fais quoi en ce moment ?",
    4  => "La journee s est bien passee ?",
    5  => "Tu as mange ?",
    6  => "Des nouvelles a partager ?",
    7  => "Tu bosses sur quoi ces derniers temps ?",
    8  => "Toujours la ?",
    9  => "On se capte bientot !",
    10 => "Prends soin de toi.",
    11 => "T as vu les infos aujourd hui ?",
    12 => "Ouais un peu, pourquoi ?",
    13 => "Rien de special, juste curieux.",
    14 => "Ah ok, moi j ai ete occupe toute la journee.",
    15 => "C etait quoi comme journee ?",
    16 => "Pas mal, reunions le matin et apres-midi libre.",
    17 => "Cool ! Moi pareil, on se ressemble haha.",
    18 => "Haha oui ! Bon allez je te laisse.",
    19 => "Ok, bonne soiree a toi !",
    20 => "Merci, toi aussi, a bientot !",
];

// ─── HTTP ─────────────────────────────────────────────────────────────────────
function http_get(string $path, array $params = []): array {
    $params['key'] = API_KEY;
    $url = BASE_URL . $path . '?' . http_build_query($params);
    $ch  = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 25,
        CURLOPT_HTTPHEADER     => ['Accept: application/json'],
    ]);
    $res  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if (!$res || $code < 200 || $code >= 300) throw new RuntimeException("HTTP $code $path");
    return json_decode($res, true) ?: [];
}

function http_post_form(string $path, array $data): array {
    $data['key'] = API_KEY;
    $ch = curl_init(BASE_URL . $path);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => http_build_query($data),
        CURLOPT_TIMEOUT        => 25,
        CURLOPT_HTTPHEADER     => ['Accept: application/json'],
    ]);
    $res  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if (!$res) throw new RuntimeException("cURL error $path");
    $json = json_decode($res, true) ?: [];
    if (isset($json['success']) && $json['success'] === false) {
        $err = is_array($json['error']) ? ($json['error']['message'] ?? 'erreur') : (string)$json['error'];
        throw new RuntimeException($err);
    }
    return $json;
}

// ─── SIMs ─────────────────────────────────────────────────────────────────────
function fetch_sims(): array {
    $data = http_get('/services/get-devices.php');
    $out  = [];
    $skip = [];
    foreach (($data['data']['devices'] ?? []) as $dev) {
        $did = $dev['id'] ?? null;
        foreach (($dev['sims'] ?? []) as $slot => $label) {
            preg_match('/\[([^\]]+)\]/', $label, $m);
            $num = trim($m[1] ?? $label);
            if ($did && preg_match('/^\+\d{7,15}$/', $num)) {
                $out[$num] = "{$did}|{$slot}";
            } else {
                $skip[] = $label;
            }
        }
    }
    if ($skip) log_("Ignores: " . implode(', ', $skip));
    return $out;
}

// ─── SEND ─────────────────────────────────────────────────────────────────────
function send_sms(string $spec, string $to, string $msg): void {
    http_post_form('/services/send.php', [
        'number'     => $to,
        'message'    => $msg,
        'devices'    => $spec,
        'type'       => 'sms',
        'prioritize' => 1,
    ]);
    log_("  >> $spec -> $to : $msg");
}

// ─── STATE ────────────────────────────────────────────────────────────────────
function load_state(): array {
    if (!file_exists(STATE_FILE)) return ['rr_idx' => 0, 'done_pairs' => []];
    return json_decode(file_get_contents(STATE_FILE), true) ?: ['rr_idx' => 0, 'done_pairs' => []];
}

function save_state(array $s): void {
    $tmp = STATE_FILE . '.tmp';
    file_put_contents($tmp, json_encode($s, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    rename($tmp, STATE_FILE);
}

function log_(string $msg): void {
    echo '[' . date('H:i:s') . '] ' . $msg . PHP_EOL;
}

function tpl(int $turn): string {
    $idx = (($turn - 1) % count(TEMPLATES)) + 1;
    return TEMPLATES[$idx];
}

// ─── CONVERSATION COMPLETE A <-> B ────────────────────────────────────────────
function run_conversation(array $sims, string $numA, string $numB): void {
    $specA = $sims[$numA];
    $specB = $sims[$numB];

    log_("=== CONV $numA <-> $numB (" . MAX_TURNS . " tours) ===");

    for ($turn = 1; $turn <= MAX_TURNS; $turn++) {
        // Tour impair : A envoie a B
        // Tour pair   : B repond a A
        if ($turn % 2 === 1) {
            $sender_spec = $specA;
            $sender_num  = $numA;
            $target_num  = $numB;
        } else {
            $sender_spec = $specB;
            $sender_num  = $numB;
            $target_num  = $numA;
        }

        $msg = tpl($turn);

        try {
            send_sms($sender_spec, $target_num, $msg);
        } catch (Exception $e) {
            log_("  [ERR] tour $turn $sender_num->$target_num : " . $e->getMessage());
        }

        // Delai entre chaque message (sauf apres le dernier)
        if ($turn < MAX_TURNS) {
            $delay = random_int(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S);
            log_("  [attente {$delay}s avant tour " . ($turn + 1) . "]");
            sleep($delay);
        }
    }

    log_("=== FIN CONV $numA <-> $numB ===");
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
function run(): void {
    if (!API_KEY) { fwrite(STDERR, "SMS_GATEWAY_API_KEY manquant\n"); exit(1); }

    log_("AutoChat ExaGate PHP — Full Auto Round-Robin");
    log_("BASE_URL = " . BASE_URL);
    log_("MAX_TURNS=" . MAX_TURNS . " REPLY_DELAY=" . REPLY_DELAY_MIN_S . "-" . REPLY_DELAY_MAX_S . "s");

    // Test connexion
    try {
        http_get('/services/get-devices.php');
        log_("[INIT] connexion OK");
    } catch (Exception $e) {
        fwrite(STDERR, "Connexion impossible: " . $e->getMessage() . "\n"); exit(1);
    }

    $state = (getenv('RESET_STATE') === '1') ? ['rr_idx' => 0, 'done_pairs' => []] : load_state();
    if (getenv('RESET_STATE') === '1') log_("[INIT] RESET_STATE=1 — etat vierge");

    log_("Demarrage dans 3s...\n");
    sleep(3);

    while (true) {
        // Recuperer SIMs frais
        try {
            $sims = fetch_sims();
        } catch (Exception $e) {
            log_("[ERR] fetch_sims: " . $e->getMessage());
            sleep(30);
            continue;
        }

        if (count($sims) < 2) {
            log_("[WARN] < 2 SIMs, attente...");
            sleep(30);
            continue;
        }

        $nums = array_keys($sims);
        sort($nums);

        log_("[SIMS] " . count($nums) . ": " . implode(', ', $nums));

        // Index round-robin : le SIM emetteur du cycle
        $rr_idx = $state['rr_idx'] % count($nums);
        $sender = $nums[$rr_idx];
        $targets = array_values(array_filter($nums, fn($n) => $n !== $sender));

        log_("[RR] Emetteur: $sender (idx=$rr_idx) -> " . count($targets) . " targets");

        // Executer une conversation complete avec chaque target
        foreach ($targets as $target) {
            $arr = [$sender, $target];
            sort($arr);
            $pair_key = implode('|', $arr);

            log_("[RR] Paire: $sender <-> $target");

            run_conversation($sims, $sender, $target);

            // Petit delai entre chaque paire
            $d = random_int(PAIR_DELAY_MIN_S, PAIR_DELAY_MAX_S);
            log_("[PAUSE entre paires: {$d}s]");
            sleep($d);

            save_state($state);
        }

        // Passer au SIM suivant
        $state['rr_idx'] = ($rr_idx + 1) % count($nums);
        save_state($state);

        if ($state['rr_idx'] === 0) {
            log_("\n[RR] Cycle complet ! Pause " . RR_PAUSE_S . "s avant nouveau cycle\n");
            sleep(RR_PAUSE_S);
        } else {
            log_("[RR] Passage au SIM suivant: " . $nums[$state['rr_idx']]);
            sleep(5);
        }
    }
}

run();
