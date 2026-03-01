<?php
/**
 * AutoChat ExaGate — Toutes paires SIMULTANÉES
 * =============================================
 * Toutes les combinaisons A<->B sont lancées en même temps
 * via pcntl_fork. Pas de round-robin séquentiel.
 *
 * Ex. avec 4 SIMs (A,B,C,D) : 6 conversations en parallèle
 *   A<->B  A<->C  A<->D  B<->C  B<->D  C<->D
 */

// ─── SERVEUR HTTP MINIMAL (pour Render Web Service) ──────────────────────────
if (function_exists('pcntl_fork')) {
    $pid = pcntl_fork();
    if ($pid === 0) {
        $port = getenv('PORT') ?: 10000;
        $sock = @stream_socket_server("tcp://0.0.0.0:{$port}", $errno, $errstr);
        if ($sock) {
            while (true) {
                $conn = @stream_socket_accept($sock, 5);
                if ($conn) {
                    fwrite($conn, "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK");
                    fclose($conn);
                }
            }
        }
        exit(0);
    }
}

define('BASE_URL',          rtrim(getenv('SMS_GATEWAY_URL')   ?: 'https://gate.exanewtech.com', '/'));
define('API_KEY',           getenv('SMS_GATEWAY_API_KEY')     ?: '');
define('MAX_TURNS',         (int)(getenv('MAX_TURNS')         ?: 20));
define('REPLY_DELAY_MIN_S', (int)(getenv('REPLY_DELAY_MIN_S') ?: 1));
define('REPLY_DELAY_MAX_S', (int)(getenv('REPLY_DELAY_MAX_S') ?: 2));

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

// ─── UTILS ───────────────────────────────────────────────────────────────────
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
        if ($turn % 2 === 1) {
            [$sender_spec, $sender_num, $target_num] = [$specA, $numA, $numB];
        } else {
            [$sender_spec, $sender_num, $target_num] = [$specB, $numB, $numA];
        }

        try {
            send_sms($sender_spec, $target_num, tpl($turn));
        } catch (Exception $e) {
            log_("  [ERR] tour $turn $sender_num->$target_num : " . $e->getMessage());
        }

        if ($turn < MAX_TURNS) {
            $delay = random_int(REPLY_DELAY_MIN_S, REPLY_DELAY_MAX_S);
            log_("  [attente {$delay}s avant tour " . ($turn + 1) . "]");
            sleep($delay);
        }
    }

    log_("=== FIN CONV $numA <-> $numB ===");
}

// ─── GÉNÈRE TOUTES LES COMBINAISONS DE PAIRES ────────────────────────────────
function all_pairs(array $nums): array {
    $pairs = [];
    $n = count($nums);
    for ($i = 0; $i < $n; $i++) {
        for ($j = $i + 1; $j < $n; $j++) {
            $pairs[] = [$nums[$i], $nums[$j]];
        }
    }
    return $pairs;
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
function run(): void {
    if (!API_KEY) { fwrite(STDERR, "SMS_GATEWAY_API_KEY manquant\n"); exit(1); }

    log_("AutoChat ExaGate PHP — Toutes paires SIMULTANÉES");
    log_("BASE_URL = " . BASE_URL);
    log_("MAX_TURNS=" . MAX_TURNS . " REPLY_DELAY=" . REPLY_DELAY_MIN_S . "-" . REPLY_DELAY_MAX_S . "s");

    try {
        http_get('/services/get-devices.php');
        log_("[INIT] connexion OK");
    } catch (Exception $e) {
        fwrite(STDERR, "Connexion impossible: " . $e->getMessage() . "\n"); exit(1);
    }

    log_("Demarrage dans 3s...\n");
    sleep(3);

    while (true) {
        // Récupérer les SIMs
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

        $nums  = array_keys($sims);
        sort($nums);
        $pairs = all_pairs($nums);

        log_("[SIMS] " . count($nums) . ": " . implode(', ', $nums));
        log_("[PAIRS] " . count($pairs) . " conversations a lancer en parallele");

        // ── Fork une conversation par paire ──────────────────────────────────
        $pids = [];
        foreach ($pairs as [$numA, $numB]) {
            $pid = pcntl_fork();
            if ($pid === -1) {
                // Pas de fork disponible → fallback séquentiel
                log_("[WARN] fork impossible, sequentiel pour $numA <-> $numB");
                run_conversation($sims, $numA, $numB);
            } elseif ($pid === 0) {
                // Fils : exécuter la conversation et quitter
                run_conversation($sims, $numA, $numB);
                exit(0);
            } else {
                // Parent : enregistrer le PID
                $pids[$pid] = "$numA <-> $numB";
                log_("[FORK] PID $pid → $numA <-> $numB");
            }
        }

        // ── Attendre la fin de tous les fils ─────────────────────────────────
        foreach ($pids as $pid => $label) {
            pcntl_waitpid($pid, $status);
            log_("[DONE] PID $pid ($label) terminé");
        }

        log_("\n[CYCLE] Toutes les conversations terminées.");

        // Relancer un nouveau cycle ou s'arrêter selon besoin
        $pause = (int)(getenv('CYCLE_PAUSE_S') ?: 60);
        if ($pause > 0) {
            log_("[PAUSE] Prochain cycle dans {$pause}s...\n");
            sleep($pause);
        } else {
            log_("[FIN] CYCLE_PAUSE_S=0, arrêt.");
            exit(0);
        }
    }
}

run();
