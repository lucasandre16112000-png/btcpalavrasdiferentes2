#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""
realfindbitcoin.py
Versão final adaptada: usa mempool.space + blockchair, parallel 10+2 & 11+1,
rate-limiter por-API com caps, checkpoint tolerante (int/float), contadores restaurados.
Mantém a lógica determinística (não pula combinações).

Corrigido: Ajustado time.sleep dos produtores para iniciar a execução de forma mais robusta.
"""

import os
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
import hmac
import hashlib
from mnemonic import Mnemonic
# As bibliotecas 'bitcoin' estão obsoletas e desaconselhadas para novos projetos.
# No entanto, mantive a lógica original, que provavelmente é importada do "pybitcointools" (que também está em desuso).
# Se precisar de um código de produção, é altamente recomendável migrar para bibliotecas como 'hdwallet' ou 'bip39' + 'ecdsa'.
from bitcoin import privtopub, pubtoaddr, encode_privkey


# -------------------------
# CONFIGURAÇÃO (edite se quiser)
# -------------------------
MODE = "hybrid"  # '10+2', '11+1', or 'hybrid' (10+2 runs first if hybrid)
mnemo = Mnemonic('english')
WORDLIST = mnemo.wordlist

# APIs: (url_template, initial_tps, max_tps)
API_DEFINITIONS = [
    ("https://api.blockchair.com/bitcoin/dashboards/address/{}", 2.0, 2.5),  # Blockchair (conservador)
    ("https://mempool.space/api/address/{}", 1.8, 2.0),                      # Mempool.space
]

CHECKPOINT_FILE = "checkpoint.txt"  # kept name compatible with your original
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"

# Concurrency
MAX_WORKERS = 3
MAX_INFLIGHT_TASKS = MAX_WORKERS * 3

# Retries / backoff
MAX_RETRIES = 6
BACKOFF_BASE = 2.0
JITTER = 0.25

# Adaptive thresholds
RATE_ADJUST_WINDOW = 30.0
GLOBAL_429_THRESHOLD = 8
GLOBAL_PAUSE_SECONDS = 20.0

# Status interval
STATUS_INTERVAL = 15.0


# -------------------------
# UTILITÁRIOS: checkpoint compatível e tolerante
# -------------------------
def save_checkpoint(pattern, base_word, var1_idx, var2_idx, counts=None):
    """
    Save checkpoint in a tolerant format:
      pattern (10+2/11+1), base_word, var1_idx, var2_idx, timestamp
      opcionalmente anexando contadores TOTAL_TESTS, VALID_BIP39_COUNT, FOUND_WITH_BALANCE
    """
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            f.write(f"{pattern}\n{base_word}\n{var1_idx}\n{var2_idx}\n{time.time()}\n")
            if counts:
                f.write(f"{counts.get('TOTAL_TESTS',0)}\n{counts.get('VALID_BIP39_COUNT',0)}\n{counts.get('FOUND_WITH_BALANCE',0)}\n")
    except Exception as e:
        print(f"❌ Erro ao salvar checkpoint: {e}")


def load_checkpoint():
    """
    Returns (pattern, base_word, var1_idx, var2_idx, counts_dict_or_None)
    Accepts old formats and tolerates floats for numeric fields.
    """
    if not os.path.exists(CHECKPOINT_FILE):
        return None, None, 0, 0, None
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
            if len(lines) >= 4:
                pattern = lines[0]
                base_word = lines[1]
                # parse integers tolerant to floats
                def to_int_safe(s):
                    try:
                        return int(s)
                    except Exception:
                        try:
                            return int(float(s))
                        except Exception:
                            return 0
                var1_idx = to_int_safe(lines[2])
                var2_idx = to_int_safe(lines[3])
                counts = None
                if len(lines) >= 7:
                    try:
                        counts = {
                            "TOTAL_TESTS": to_int_safe(lines[4]),
                            "VALID_BIP39_COUNT": to_int_safe(lines[5]),
                            "FOUND_WITH_BALANCE": to_int_safe(lines[6])
                        }
                    except Exception:
                        counts = None
                return pattern, base_word, var1_idx, var2_idx, counts
    except Exception as e:
        print(f"❌ Erro ao carregar checkpoint: {e}")
    return None, None, 0, 0, None


def save_last_combo(mnemonic):
    try:
        with open(ULTIMO_FILE, "w", encoding="utf-8") as f:
            f.write(mnemonic + "\n")
    except Exception as e:
        print(f"❌ Erro ao salvar ultimo mnemonic: {e}")


def save_wallet_with_balance(mnemonic, address, wif, hex_key, pub_key, balance, api_name):
    try:
        # Usando 'current_pattern' da variável global para salvar nos logs.
        current_patt = globals().get('current_pattern', 'N/A')
        with open(SALDO_FILE, "a", encoding="utf-8") as f:
            f.write("="*80 + "\n")
            f.write("💎 CARTEIRA COM SALDO ENCONTRADA\n")
            f.write(f"Pattern: {current_patt}\n")
            f.write(f"Mnemonic: {mnemonic}\n")
            f.write(f"Endereço: {address}\n")
            f.write(f"Saldo: {balance:.8f} BTC (API: {api_name})\n")
            f.write(f"WIF: {wif}\n")
            f.write(f"PrivHex: {hex_key}\n")
            f.write(f"PubKey: {pub_key}\n")
            f.write("-"*80 + "\n\n")
    except Exception as e:
        print(f"❌ Erro ao salvar carteira com saldo: {e}")


# -------------------------
# API limiter (token bucket) with cap at max_rate
# -------------------------
class ApiLimiter:
    def __init__(self, initial_rate, max_rate):
        self.rate = float(max(0.01, initial_rate))
        self.max_rate = float(max_rate)
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.last = time.time()
        self.lock = threading.Lock()
        self.recent_429 = []

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                delta = now - self.last
                if delta > 0:
                    self.tokens = min(self.capacity, self.tokens + delta * self.rate)
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
        with self.lock:
            recent = self.get_recent_429_count()
            if recent >= 3:
                new_rate = max(0.05, self.rate * 0.35)
            elif recent > 0:
                new_rate = max(0.05, self.rate * 0.6)
            else:
                new_rate = min(self.max_rate, self.rate * 1.04 + 0.01)
            if abs(new_rate - self.rate) / max(self.rate, 1e-9) > 0.01:
                prop = self.tokens / self.capacity if self.capacity > 0 else 1.0
                self.rate = new_rate
                self.capacity = max(1.0, self.rate)
                self.tokens = min(self.capacity, prop * self.capacity)


API_LIMITERS = [ApiLimiter(init, maxr) for (_, init, maxr) in API_DEFINITIONS]
GLOBAL_429_HISTORY = []
GLOBAL_429_LOCK = threading.Lock()


def global_note_429():
    with GLOBAL_429_LOCK:
        ts = time.time()
        GLOBAL_429_HISTORY.append(ts)
        cutoff = ts - RATE_ADJUST_WINDOW
        GLOBAL_429_HISTORY[:] = [t for t in GLOBAL_429_HISTORY if t >= cutoff]


def get_global_429_count():
    with GLOBAL_429_LOCK:
        ts = time.time()
        cutoff = ts - RATE_ADJUST_WINDOW
        GLOBAL_429_HISTORY[:] = [t for t in GLOBAL_429_HISTORY if t >= cutoff]
        return len(GLOBAL_429_HISTORY)


# -------------------------
# HTTP session
# -------------------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({"User-Agent": "realfindbitcoin/1.0"})


# -------------------------
# key derivation (kept compatible)
# -------------------------
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


# -------------------------
# API query logic (supports blockchair + mempool.space)
# -------------------------
def extract_balance_from_blockchair_json(data, address):
    """
    Blockchair typical structure.
    """
    try:
        d = data.get("data", {})
        if address in d:
            addr_info = d[address].get("address", {})
            bal = addr_info.get("balance")
            if bal is None:
                bal = d[address].get("balance")
            return int(bal) if bal is not None else 0
    except Exception:
        pass
    # fallback scan
    try:
        for v in data.get("data", {}).values():
            if isinstance(v, dict):
                for sub in v.values():
                    if isinstance(sub, dict) and 'balance' in sub:
                        return int(sub.get('balance', 0) or 0)
    except Exception:
        pass
    return 0


def extract_balance_from_mempool_json(data):
    """
    mempool.space structure.
    """
    try:
        chain = data.get("chain_stats", {})
        funded = chain.get("funded_txo_sum", 0)
        spent = chain.get("spent_txo_sum", 0)
        return max(0, int(funded) - int(spent))
    except Exception:
        pass
    # fallback
    try:
        return int(data.get("balance", 0) or 0)
    except Exception:
        return 0


def query_apis_for_address(address):
    """
    Return (True, balance_btc, api_name) if any API reports balance > 0.
    """
    for idx, (url_template, _, _) in enumerate(API_DEFINITIONS):
        limiter = API_LIMITERS[idx]
        api_name = url_template.split("//")[1].split("/")[0]
        for attempt in range(MAX_RETRIES):
            # global pause if many recent 429s
            if get_global_429_count() >= GLOBAL_429_THRESHOLD:
                print(f"🟠 Muitas 429s globais ({get_global_429_count()}) — pausa global {int(GLOBAL_PAUSE_SECONDS)}s.")
                time.sleep(GLOBAL_PAUSE_SECONDS)
                for lim in API_LIMITERS:
                    lim.adjust_rate()

            limiter.acquire()
            try:
                url = url_template.format(address)
                resp = session.get(url, timeout=12)
                if resp.status_code == 429:
                    limiter.note_429()
                    global_note_429()
                    retry_after = None
                    if 'Retry-After' in resp.headers:
                        try:
                            retry_after = float(resp.headers.get('Retry-After'))
                        except Exception:
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
                if "blockchair.com" in url_template:
                    balance_satoshi = extract_balance_from_blockchair_json(data, address)
                elif "mempool.space" in url_template:
                    balance_satoshi = extract_balance_from_mempool_json(data)
                else:
                    # general fallback
                    balance_satoshi = data.get('final_balance', data.get('balance', 0) or 0)

                balance_btc = balance_satoshi / 1e8 if balance_satoshi else 0.0
                if balance_btc > 0:
                    return True, balance_btc, api_name
                # zero balance from this API: try next
                break
            except requests.exceptions.RequestException as e:
                sleep_time = (BACKOFF_BASE ** attempt) * (0.2 + random.random() * JITTER)
                print(f"❌ Erro de conexão em {api_name}: {e}. Retentando em {sleep_time:.2f}s (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(sleep_time)
                continue
            except ValueError:
                # E.g., JSON decode error
                break
    return False, 0.0, None


# -------------------------
# Counters (thread-safe)
# -------------------------
VALID_BIP39_COUNT = 0
FOUND_WITH_BALANCE = 0
TOTAL_TESTS = 0
lock_counters = threading.Lock()


def check_wallet_balance_mnemonic(mnemonic):
    global VALID_BIP39_COUNT, FOUND_WITH_BALANCE, TOTAL_TESTS
    # validate mnemonic
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


# -------------------------
# Deterministic iterators
# -------------------------
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


# -------------------------
# Producers & orchestration
# -------------------------
inflight_semaphore = threading.Semaphore(MAX_INFLIGHT_TASKS)
futures_set_lock = threading.Lock()
futures_set = set()


def _task_wrapper(mnemonic):
    try:
        return check_wallet_balance_mnemonic(mnemonic)
    finally:
        try:
            inflight_semaphore.release()
        except Exception:
            pass


def producer_10_plus_2(start_base_idx, start_j, start_k, executor, stop_event):
    global current_pattern
    current_pattern = "10+2"
    for i, j, k, mnemonic in iter_10_plus_2_from(start_base_idx, start_j, start_k):
        if stop_event.is_set():
            break
        
        # --- CORREÇÃO/MELHORIA 1: Aumentar o sleep para 10ms ---
        # Isso garante que a thread produtora não monopolize a CPU e cede tempo para o
        # escalonador de threads, permitindo que a thread principal e os workers iniciem.
        time.sleep(0.01)
        
        inflight_semaphore.acquire()
        try:
            save_last_combo(mnemonic)
            save_checkpoint("10+2", WORDLIST[i], j, k, {
                "TOTAL_TESTS": TOTAL_TESTS,
                "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
            })
        except Exception:
            pass
        fut = executor.submit(_task_wrapper, mnemonic)
        with futures_set_lock:
            futures_set.add(fut)
        
    return


def producer_11_plus_1(start_base_idx, start_j, executor, stop_event):
    global current_pattern
    current_pattern = "11+1"
    for i, j, mnemonic in iter_11_plus_1_from(start_base_idx, start_j):
        if stop_event.is_set():
            break
        
        # --- CORREÇÃO/MELHORIA 1: Aumentar o sleep para 10ms ---
        time.sleep(0.01)
        
        inflight_semaphore.acquire()
        try:
            save_last_combo(mnemonic)
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
        
    return


# -------------------------
# Main
# -------------------------
def main():
    global current_pattern, MODE
    print("Inicializando realfindbitcoin.py (final)")
    ck_pattern, ck_base_word, ck_var1_idx, ck_var2_idx, counts = load_checkpoint()

    # determine base idx
    if ck_base_word:
        try:
            base_idx = WORDLIST.index(ck_base_word)
        except ValueError:
            # Word not found, reset to start of list
            base_idx = 0
            ck_pattern = None # force re-determination of starting pattern
            ck_var1_idx = 0
            ck_var2_idx = 0
            print(f"⚠️ Base word '{ck_base_word}' not found in wordlist. Resetting search to start.")
    else:
        base_idx = 0

    # restore counters if present
    global TOTAL_TESTS, VALID_BIP39_COUNT, FOUND_WITH_BALANCE
    if counts:
        TOTAL_TESTS = counts.get("TOTAL_TESTS", TOTAL_TESTS)
        VALID_BIP39_COUNT = counts.get("VALID_BIP39_COUNT", VALID_BIP39_COUNT)
        FOUND_WITH_BALANCE = counts.get("FOUND_WITH_BALANCE", FOUND_WITH_BALANCE)
        print(f"✅ Checkpoint carregado. Base Word: {ck_base_word} | Padrão: {ck_pattern} | Testes: {TOTAL_TESTS}")


    # decide pattern order: resume pattern from checkpoint first, then other if hybrid
    if ck_pattern in ("10+2", "11+1"):
        patterns = [ck_pattern]
        # CORREÇÃO: A base_idx deve ser a mesma para ambos os padrões se o primeiro
        # for concluído. Mas para o modo 'hybrid', queremos continuar a partir daquele ponto
        # e *depois* fazer o outro.
        if MODE == "hybrid":
            other_pattern = "11+1" if ck_pattern == "10+2" else "10+2"
            patterns.append(other_pattern)
    else:
        # If no valid checkpoint or base_word not found, start from beginning based on MODE
        patterns = ["10+2", "11+1"] if MODE == "hybrid" else ([MODE] if MODE in ("10+2", "11+1") else ["10+2", "11+1"])
        ck_var1_idx = 0
        ck_var2_idx = 0


    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    stop_event = threading.Event()
    producer_threads = []

    try:
        current_base_idx = base_idx
        for i, pat in enumerate(patterns):
            # No primeiro padrão, usa o índice de base e os índices var do checkpoint
            if i == 0 and pat == ck_pattern:
                start_base_idx = current_base_idx
                start_j = ck_var1_idx
                start_k = ck_var2_idx
            else:
                # Nos padrões subsequentes ou se o ck não for válido, começa do zero ou
                # da base_idx atual, mas com var1/var2_idx em 0
                start_base_idx = current_base_idx
                start_j = 0
                start_k = 0
                
            print(f"▶️ Iniciando o produtor para o padrão: {pat} | Base Word ID: {start_base_idx} ({WORDLIST[start_base_idx]}) | Var1 Index: {start_j}")


            if pat == "10+2":
                t = threading.Thread(target=producer_10_plus_2, args=(start_base_idx, start_j, start_k, executor, stop_event), daemon=True)
                producer_threads.append(t)
            else: # "11+1"
                t = threading.Thread(target=producer_11_plus_1, args=(start_base_idx, start_j, executor, stop_event), daemon=True)
                producer_threads.append(t)


        for t in producer_threads:
            t.start()


        last_status = time.time()
        while any(t.is_alive() for t in producer_threads) or futures_set:
            # collect finished futures
            done_list = []
            with futures_set_lock:
                for f in list(futures_set):
                    if f.done():
                        done_list.append(f)
                        futures_set.remove(f)
            for f in done_list:
                try:
                    _ = f.result()
                except Exception as e:
                    print(f"❌ Erro em task: {e}")

            # adjust API rates
            for lim in API_LIMITERS:
                lim.adjust_rate()

            # periodic status
            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                with lock_counters:
                    rates = ", ".join([f"{API_DEFINITIONS[idx][0].split('//')[1].split('/')[0]}:{round(API_LIMITERS[idx].rate,3)}t/s" for idx in range(len(API_DEFINITIONS))])
                    inflight_used = MAX_INFLIGHT_TASKS - (inflight_semaphore._value if hasattr(inflight_semaphore,'_value') else 0)
                    
                    # CORREÇÃO: Usar WORDLIST[current_base_idx] para mostrar o progresso real
                    current_idx_info = f"Base: {WORDLIST[base_idx]}" if base_idx < len(WORDLIST) else "Base: END"
                    current_patt = globals().get('current_pattern', 'N/A')
                    
                    print(f"\n[STATUS: {current_patt} | {current_idx_info}]")
                    print(f"  TOTAL_TESTS={TOTAL_TESTS} | VALID_BIP39={VALID_BIP39_COUNT} | FOUND_WITH_BALANCE={FOUND_WITH_BALANCE} | inflight={inflight_used}")
                    print(f"  API rates: {rates} | Global 429s recent: {get_global_429_count()}")
                
                # save checkpoint with counters (using the current global pattern)
                try:
                    patt = current_patt if current_patt != 'N/A' else patterns[0]
                    # Nota: O checkpoint aqui pode não refletir os índices var1/var2 exatos,
                    # mas o produtor salvará o checkpoint exato a cada iteração. Este é um fallback.
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
        print("\n🛑 Interrompido pelo usuário — salvando checkpoint e saindo...")
        stop_event.set()
    finally:
        stop_event.set()
        for t in producer_threads:
            t.join(timeout=2.0)
        # wait a moment for inflight tasks to finish
        wait_start = time.time()
        while futures_set and time.time() - wait_start < 10.0:
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
        executor.shutdown(wait=False)
        # final checkpoint
        try:
            # Usa o último padrão conhecido ou o primeiro da lista
            patt = globals().get('current_pattern', patterns[0]) if patterns else "N/A"
            save_checkpoint(patt, WORDLIST[base_idx], 0, 0, {
                "TOTAL_TESTS": TOTAL_TESTS,
                "VALID_BIP39_COUNT": VALID_BIP39_COUNT,
                "FOUND_WITH_BALANCE": FOUND_WITH_BALANCE
            })
        except Exception:
            pass

        print("\n--- EXECUÇÃO FINALIZADA ---")
        print(f"TOTAL_TESTS = {TOTAL_TESTS}")
        print(f"VALID_BIP39_COUNT = {VALID_BIP39_COUNT}")
        print(f"FOUND_WITH_BALANCE = {FOUND_WITH_BALANCE}")


if __name__ == "__main__":
    # initialize checkpoint if missing to ensure hybrid order resumes
    if not os.path.exists(CHECKPOINT_FILE):
        initial_pattern = "10+2" if MODE == "hybrid" else (MODE if MODE in ("10+2", "11+1") else "10+2")
        save_checkpoint(initial_pattern, WORDLIST[0], 0, 0, {
            "TOTAL_TESTS": 0, "VALID_BIP39_COUNT": 0, "FOUND_WITH_BALANCE": 0
        })
    current_pattern = None
    main()
