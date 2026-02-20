<?php
/**
 * AutoChat ExaGate — Round-Robin Broadcast
 * ==========================================
 * Inspiré de sms_auto_reply.php et generate_device_config.php
 *
 * Usage CLI : php autochat.php
 * Render    : start command = php autochat.php
 *
 * Variables d'environnement :
 *   SMS_GATEWAY_API_KEY   (requis)
 *   SMS_GATEWAY_URL       (defaut: https://gate.exanewtech.com)
 *   MAX_TURNS             (defaut: 10)
 *   POLL_INTERVAL_S       (defaut: 5)
 *   REPLY_DELAY_MIN_S     (defaut: 3)
 *   REPLY_DELAY_MAX_S     (defaut: 5)
 *   RR_TICK_S             (defaut: 20)
 *   RESET_STATE           (mettre 1 pour vider l'etat au demarrage)
 */

// ─── CONFIG ────────────────────────────────────────────────────────────────────
define('BASE_URL',          rtrim(getenv('SMS_GATEWAY_URL')   ?: 'https://gate.exanewtech.com', '/'));
define('API_KEY',           getenv('SMS_GATEWAY_API_KEY')     ?: '');
define('STATE_FILE',        getenv('STATE_FILE')              ?: __DIR__ . '/state.json');
define('MAX_TURNS',         (int)(getenv('MAX_TURNS')         ?: 10));
define('POLL_INTERVAL_S',   (int)(getenv('POLL_INTERVAL_S')   ?: 5));
define('RR_TICK_S',         (int)(getenv('RR_TICK_S')         ?: 20));
define('SIM_REFRESH_S',     (int)(getenv('SIM_REFRESH_S')     ?: 120));
define('REPLY_DELAY_MIN_S', (int)(getenv('REPLY_DELAY_MIN_S') ?: 3));
define('REPLY_DELAY_MAX_S', (int)(getenv('REPLY_DELAY_MAX_S') ?: 5));

const TEMPLATES = [
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
];

// ─── HTTP ───────────────────────────────────────────────────────────────────────
function http_get(string $path, array $params = []): array {
    $params['key'] = API_KEY;
    $url = BASE_URL . $path . '?' . http_build_query($params);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 25,
        CURLOPT_HTTPHEADER     => ['Accept: application/json'],
    ]);
    $res  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($res === false || $code < 200 || $code >= 300) {
        throw new RuntimeException("HTTP $code sur $path");
    }
    $json = json_decode($res, true);
    return is_array($json) ? $json : [];
}

function http_post(string $path, array $data): array {
    $data['key'] = API_KEY;
    $url = BASE_URL . $path;

    $ch = curl_init($url);
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

    if ($res === false) throw new RuntimeException("cURL error sur $path");
    $json = json_decode($res, true);
    if (is_array($json) && isset($json['success']) && $json['success'] === false) {
        $err = $json['error']['message'] ?? ($json['error'] ?? 'Erreur inconnue');
        throw new RuntimeException((string)$err);
    }
    return is_array($json) ? $json : [];
}

// ─── SIMs ───────────────────────────────────────────────────────────────────────
function fetch_sims(): array {
    // Retourne ['+33...' => 'deviceID|slot', ...]
    $data = http_get('/services/get-devices.php');
    $out  = [];
    $skip = [];

    foreach (($data['data']['devices'] ?? []) as $dev) {
        $did = $dev['id'] ?? null;
        foreach (($dev['sims'] ?? []) as $slot => $label) {
            // Extraire le numero entre crochets : "Free [+33782801240]"
            if (preg_match('/\[([^\]]+)\]/', $label, $m)) {
                $num = trim($m[1]);
            } else {
                $num = trim($label);
            }
            // Valider format E.164
            if ($did && preg_match('/^\+\d{7,15}$/', $num)) {
                $out[$num] = "{$did}|{$slot}";
            } else {
                $skip[] = $label;
            }
        }
    }
    if ($skip) {
        log_line("[SIMS] Ignores: " . implode(', ', $skip));
    }
    return $out;
}

// ─── SEND ───────────────────────────────────────────────────────────────────────
function send_sms(string $spec, string $to, string $msg): void {
    http_post('/services/send.php', [
        'number'     => $to,
        'message'    => $msg,
        'devices'    => $spec,
        'type'       => 'sms',
        'prioritize' => 1,
    ]);
    log_line("  [SMS] $spec -> $to: " . substr($msg, 0, 55));
}

// ─── MESSAGES RECUS ─────────────────────────────────────────────────────────────
function fetch_received(): array {
    $data = http_get('/services/get-messages.php', ['status' => 'Received']);
    if (empty($data['success'])) return [];
    return $data['data']['messages'] ?? [];
}

function msg_id(array $m): string {
    $id = $m['id'] ?? $m['ID'] ?? null;
    if ($id) return (string)$id;
    return ($m['number'] ?? '') . '-' . substr($m['message'] ?? '', 0, 20);
}

// ─── STATE ──────────────────────────────────────────────────────────────────────
function blank_state(): array {
    return [
        'convs'   => [],   // ['sortedA|sortedB' => ['turn'=>1,'status'=>'active','last_sender'=>...]]
        'rr_idx'  => 0,
        'sims'    => [],   // ['+33...' => 'devID|slot']
        'seen'    => [],   // ['msg_id' => timestamp]
    ];
}

function load_state(): array {
    if (!file_exists(STATE_FILE)) return blank_state();
    $j = json_decode(file_get_contents(STATE_FILE), true);
    return is_array($j) ? $j : blank_state();
}

function save_state(array $state): void {
    $tmp = STATE_FILE . '.tmp';
    file_put_contents($tmp, json_encode($state, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    rename($tmp, STATE_FILE);
}

// ─── HELPERS ────────────────────────────────────────────────────────────────────
function log_line(string $msg): void {
    echo '[' . date('H:i:s') . '] ' . $msg . PHP_EOL;
}

function tpl(int $turn): string {
    return TEMPLATES[($turn - 1) % count(TEMPLATES)];
}

function conv_key(string $a, string $b): string {
    $arr = [$a, $b];
    sort($arr);
    return implode('|', $arr);
}

// ─── ROUND-ROBIN ────────────────────────────────────────────────────────────────
function cur_sender(array &$state, array $sims_list): string {
    return $sims_list[$state['rr_idx'] % count($sims_list)];
}

function sender_done(array &$state, string $sender, array $sims_list): bool {
    $targets = array_values(array_filter($sims_list, fn($n) => $n !== $sender));
    if (empty($targets)) return true;
    foreach ($targets as $t) {
        $ck   = conv_key($sender, $t);
        $conv = $state['convs'][$ck] ?? null;
        if (!$conv || ($conv['status'] ?? '') !== 'done') return false;
    }
    return true;
}

function advance_rr(array &$state, array $sims_list): string {
    $new_idx = ($state['rr_idx'] + 1) % count($sims_list);
    $state['rr_idx'] = $new_idx;
    if ($new_idx === 0) {
        $state['convs'] = [];
        $state['seen']  = [];
        log_line("[RR] Nouveau cycle — reset convs");
    }
    $next = $sims_list[$new_idx];
    log_line("[RR] Emetteur suivant -> $next (idx=$new_idx)");
    return $next;
}

function rr_tick(array &$state): array {
    $sims = $state['sims'];
    if (count($sims) < 2) return ['skip' => 'not_enough_sims'];

    $sims_list = array_keys($sims);
    sort($sims_list);
    $sender = cur_sender($state, $sims_list);

    if (sender_done($state, $sender, $sims_list)) {
        log_line("[RR] $sender termine");
        $sender = advance_rr($state, $sims_list);
    }

    $spec    = $sims[$sender];
    $targets = array_values(array_filter($sims_list, fn($n) => $n !== $sender));
    $sent = 0; $skip = 0;

    foreach ($targets as $target) {
        $ck   = conv_key($sender, $target);
        $conv = $state['convs'][$ck] ?? null;

        if ($conv === null) {
            try {
                send_sms($spec, $target, tpl(1));
                $state['convs'][$ck] = [
                    'turn'        => 1,
                    'status'      => 'active',
                    'last_sender' => $sender,
                    'at'          => time(),
                ];
                $sent++;
                usleep(random_int(1500000, 3000000)); // 1.5-3s
            } catch (Exception $e) {
                log_line("  [ERR] $sender->$target: " . $e->getMessage());
                $state['convs'][$ck] = ['turn' => 0, 'status' => 'done', 'err' => $e->getMessage()];
                $skip++;
            }
        } elseif (($conv['status'] ?? '') === 'done') {
            $skip++;
        }
    }

    $active = count(array_filter($state['convs'], fn($c) => ($c['status'] ?? '') === 'active'));
    return ['sender' => $sender, 'sent' => $sent, 'skip' => $skip, 'active' => $active];
}

// ─── MESSAGES ENTRANTS ──────────────────────────────────────────────────────────
function process(array &$state, array $msg): ?array {
    $mid      = msg_id($msg);
    $from_num = trim($msg['number'] ?? '');
    $dev_id   = $msg['deviceID'] ?? null;
    $slot     = $msg['simSlot']  ?? null;

    if (!$mid || !$from_num) return null;

    // Deduplication
    if (isset($state['seen'][$mid])) return null;
    $state['seen'][$mid] = time();
    // Nettoyage : garder seulement les 500 derniers
    if (count($state['seen']) > 500) {
        arsort($state['seen']);
        $state['seen'] = array_slice($state['seen'], 0, 400, true);
    }

    $sims = $state['sims'];

    // L'expediteur doit etre un de nos SIMs
    if (!isset($sims[$from_num])) return null;

    // Identifier le SIM recepteur via deviceID|simSlot
    $receiver_num  = null;
    $receiver_spec = null;

    if ($dev_id !== null && $slot !== null) {
        $candidate = "{$dev_id}|{$slot}";
        foreach ($sims as $num => $spec) {
            if ($spec === $candidate) {
                $receiver_num  = $num;
                $receiver_spec = $spec;
                break;
            }
        }
    }

    // Fallback : chercher la conv active qui contient from_num
    if (!$receiver_spec) {
        foreach ($state['convs'] as $ck => $conv) {
            if (($conv['status'] ?? '') !== 'active') continue;
            $parts = explode('|', $ck, 2);
            if (count($parts) !== 2) continue;
            [$a, $b] = $parts;
            if ($from_num === $a && isset($sims[$b])) {
                $receiver_num  = $b;
                $receiver_spec = $sims[$b];
                break;
            }
            if ($from_num === $b && isset($sims[$a])) {
                $receiver_num  = $a;
                $receiver_spec = $sims[$a];
                break;
            }
        }
    }

    if (!$receiver_spec || !$receiver_num) {
        log_line("  [SKIP] receiver introuvable from=$from_num dev=$dev_id slot=$slot");
        return ['skip' => 'no_receiver', 'from' => $from_num];
    }

    $ck   = conv_key($from_num, $receiver_num);
    $conv = $state['convs'][$ck] ?? [
        'turn' => 1, 'status' => 'active',
        'last_sender' => $from_num, 'at' => time()
    ];

    if (($conv['status'] ?? '') === 'done') {
        return ['skip' => 'done', 'ck' => $ck];
    }

    $turn = (int)($conv['turn'] ?? 1);
    if ($turn >= MAX_TURNS) {
        $conv['status'] = 'done';
        $state['convs'][$ck] = $conv;
        log_line("  [DONE] $ck");
        return ['done' => $ck];
    }

    $next_turn = $turn + 1;
    $reply     = tpl($next_turn);

    $delay = random_int(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S);
    sleep($delay);

    try {
        send_sms($receiver_spec, $from_num, $reply);
        $conv['turn']        = $next_turn;
        $conv['last_sender'] = $receiver_num;
        $conv['at']          = time();
        if ($next_turn >= MAX_TURNS) {
            $conv['status'] = 'done';
            log_line("  [DONE] $ck");
        }
        $state['convs'][$ck] = $conv;
        log_line("  [REPLY] $receiver_num -> $from_num tour=$next_turn");
        return ['replied' => $ck, 'turn' => $next_turn];
    } catch (Exception $e) {
        return ['err' => $e->getMessage(), 'ck' => $ck];
    }
}

// ─── MAIN ───────────────────────────────────────────────────────────────────────
function run(): void {
    if (!API_KEY) {
        fwrite(STDERR, "SMS_GATEWAY_API_KEY manquant\n");
        exit(1);
    }

    log_line("AutoChat ExaGate PHP — Round-Robin Broadcast");
    log_line("BASE_URL = " . BASE_URL);

    // Test connexion
    try {
        $r = http_get('/services/get-devices.php');
        log_line("[INIT] connexion OK -> " . ($r['success'] ? 'success' : 'echec'));
    } catch (Exception $e) {
        fwrite(STDERR, "Connexion impossible: " . $e->getMessage() . "\n");
        exit(1);
    }

    // Charger etat
    $reset = getenv('RESET_STATE') === '1';
    $state = $reset ? blank_state() : load_state();
    $state['seen'] = []; // toujours vider seen au demarrage
    log_line($reset ? "[INIT] RESET_STATE=1 — etat vierge" : "[INIT] Etat charge, seen vide");

    // Recuperer SIMs
    $sims = fetch_sims();
    if (count($sims) < 2) {
        fwrite(STDERR, "Seulement " . count($sims) . " SIM(s), minimum 2.\n");
        exit(1);
    }
    // Purger convs avec numeros invalides
    $valid = array_keys($sims);
    $state['convs'] = array_filter($state['convs'], function($ck) use ($valid) {
        foreach (explode('|', $ck, 2) as $p)
            if (!in_array($p, $valid, true)) return false;
        return true;
    }, ARRAY_FILTER_USE_KEY);
    $state['sims'] = $sims;
    save_state($state);

    log_line("[INIT] " . count($sims) . " SIMs valides:");
    foreach ($sims as $num => $spec) {
        log_line("  $num -> $spec");
    }

    log_line("\nDemarrage dans 3s... MAX_TURNS=" . MAX_TURNS . "\n");
    sleep(3);

    $last_refresh = 0.0;
    $last_tick    = 0.0;

    while (true) {
        try {
            $now = microtime(true);

            // ── Rafraichissement SIMs ──────────────────────────────────────
            if ($now - $last_refresh >= SIM_REFRESH_S) {
                try {
                    $fresh = fetch_sims();
                    if (count($fresh) >= 2) {
                        $valid = array_keys($fresh);
                        $state['convs'] = array_filter($state['convs'], function($ck) use ($valid) {
                            foreach (explode('|', $ck, 2) as $p)
                                if (!in_array($p, $valid, true)) return false;
                            return true;
                        }, ARRAY_FILTER_USE_KEY);
                        $state['sims'] = $fresh;
                        $last_refresh = $now;
                        log_line("[SIMS] " . count($fresh) . ": " . implode(', ', array_keys($fresh)));
                    }
                } catch (Exception $e) {
                    log_line("[WARN refresh] " . $e->getMessage());
                }
            }

            if (count($state['sims']) < 2) {
                sleep(15);
                continue;
            }

            // ── Messages entrants → reponse tac-a-tac ─────────────────────
            try {
                $msgs = fetch_received();
                usort($msgs, fn($a, $b) => (int)($a['id'] ?? 0) <=> (int)($b['id'] ?? 0));

                $seen_ids = $state['seen'];
                $new_msgs = array_filter($msgs, fn($m) => !isset($seen_ids[msg_id($m)]));

                if ($new_msgs) {
                    log_line("[INBOUND] " . count($new_msgs) . " nouveau(x) / " . count($msgs) . " total");
                    foreach ($new_msgs as $m) {
                        log_line("  from=" . var_export($m['number'] ?? '', true)
                            . " dev=" . ($m['deviceID'] ?? '?')
                            . " slot=" . ($m['simSlot'] ?? '?')
                            . " id=" . ($m['id'] ?? $m['ID'] ?? '?')
                            . " msg=" . var_export(substr($m['message'] ?? '', 0, 40), true));
                    }
                }

                $results = [];
                foreach ($msgs as $m) {
                    $r = process($state, $m);
                    if ($r !== null) $results[] = $r;
                }
                if ($results) {
                    log_line("[IN] " . json_encode($results, JSON_UNESCAPED_UNICODE));
                }

            } catch (Exception $e) {
                log_line("[ERR inbound] " . $e->getMessage());
            }

            // ── Tick round-robin ───────────────────────────────────────────
            if ($now - $last_tick >= RR_TICK_S) {
                try {
                    $rr = rr_tick($state);
                    $last_tick = $now;
                    if (($rr['sent'] ?? 0) > 0 || ($rr['active'] ?? 0) > 0) {
                        log_line("[RR] " . json_encode($rr, JSON_UNESCAPED_UNICODE));
                    }
                } catch (Exception $e) {
                    log_line("[ERR tick] " . $e->getMessage());
                }
            }

            save_state($state);

        } catch (Exception $e) {
            log_line("[ERR loop] " . $e->getMessage());
        }

        sleep(POLL_INTERVAL_S);
    }
}

run();
