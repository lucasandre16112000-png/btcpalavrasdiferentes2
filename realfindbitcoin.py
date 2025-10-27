#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
findbtc_otimizado_10_2.py
Gera carteiras Bitcoin (10 repetidas + 2 variáveis).
=====================================================
OTIMIZAÇÃO: Usa ThreadPoolExecutor para rodar as consultas de saldo
de forma CONCORRENTE, aplicando o delay de time.sleep(0.1) necessário
para evitar o bloqueio da API, mas sem atrasar a geração de chaves
pelo loop principal (velocidade máxima do processador).
"""

import os
import time
import random
import requests
import signal
from time import perf_counter
from concurrent.futures import ThreadPoolExecutor, as_completed # Alterado para ThreadPoolExecutor
from typing import Optional, Tuple, Dict, Any

# bip-utils imports
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator, Bip44, Bip44Coins, Bip44Changes

# -------- CONFIGURAÇÃO DE VELOCIDADE/CONCORRÊNCIA ----------
# CRÍTICO: 10 workers é o ideal para o time.sleep(0.1) / 10 requisições por segundo.
MAX_WORKERS_IO = 10 
# Tempo de espera entre as requisições para evitar o bloqueio da API (Obrigatório)
API_DELAY_SEC = 0.1 

# -------- Arquivos de Checkpoint / Log ---------------------
BIP39_WORDS_FILE = "bip39-words.txt"
CHECKPOINT_FILE = "checkpoint.txt"
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"
ESTATISTICAS_FILE = "estatisticas_finais.txt"

# Frequências de salvamento (ajustadas)
FREQUENCY_PRINT = 1000        
FREQUENCY_SAVE_CHECKPOINT = 1000 
SAVE_INTERVAL_SEC = 30       
SAVE_ULTIMO_INTERVAL = 10.0  # Salva a última combinação a cada 10s
REQUESTS_TIMEOUT = 15

# -------- I/O helpers atômicos (Preservados do seu script) ----------------
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

# -------- Carregamento / checkpoint (Preservados do seu script) ---------
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
                # Verifica se as 10 primeiras são repetidas
                if all(p == palavra_base for p in palavras[:10]):
                    return palavra_base, palavras[10], palavras[11], " ".join(palavras)
    except Exception:
        pass
    return None, None, None, None

def carregar_estatisticas_checkpoint(arquivo=CHECKPOINT_FILE):
    contador_total = contador_validas = carteiras_com_saldo = 0
    if not os.path.exists(arquivo):
        return 0, 0, 0
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

def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1, ultima_completa2):
    """Lógica do seu script 10+2: Base(i), Var1(j), Var2(j+1)"""
    try:
        base_idx = palavras.index(ultima_base)
        completa1_idx = palavras.index(ultima_completa1)
        
        # O loop principal é (base_idx, completa1_idx + 1)
        if completa1_idx + 1 < len(palavras) - 1: # -1 porque a Var2 é j+1
            return base_idx, completa1_idx + 1
        elif base_idx + 1 < len(palavras):
            return base_idx + 1, 0 # Começa Var1 do zero
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


def derivar_chaves(mnemonic: str) -> Dict[str, Any]:
    """Gera seed e deriva chaves BTC BIP44. Executado na thread principal."""
    seed_gen = Bip39SeedGenerator(mnemonic)
    seed_bytes = seed_gen.Generate()

    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
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


# -------- FUNÇÃO WORKER DE I/O CONCORRENTE (AGORA COM THREADS) ------
def verificar_saldo_api_worker(mnemonic: str, palavra_base: str, palavra_completa1: str, palavra_completa2: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Worker aprimorado contra bloqueios:
    - Retries com backoff exponencial + jitter para 429 e erros temporários.
    - Timeout configurável via REQUESTS_TIMEOUT.
    - Usa Session local à chamada para reaproveitar conexões.
    - Mantém time.sleep(API_DELAY_SEC) no final para preservar o delay anti-bloqueio.
    """
    endereco = info["address"]
    tem_saldo = False

    max_retries = 5
    base_backoff = 1.0  # segundos iniciais para backoff exponencial em 429/erros temporários
    session = requests.Session()
    headers = {
        "User-Agent": "findbtc_otimizado/1.0",
        "Accept": "application/json"
    }

    for attempt in range(1, max_retries + 1):
        try:
            url = f"https://mempool.space/api/address/{endereco}"
            response = session.get(url, headers=headers, timeout=REQUESTS_TIMEOUT)

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as e:
                    print(f"❗ JSON inválido para {endereco}: {e}")
                    data = {}

                chain = data.get("chain_stats", {}) or {}
                mempool = data.get("mempool_stats", {}) or {}

                funded_chain = int(chain.get("funded_txo_sum", 0))
                spent_chain = int(chain.get("spent_txo_sum", 0))
                funded_mempool = int(mempool.get("funded_txo_sum", 0))
                spent_mempool = int(mempool.get("spent_txo_sum", 0))

                balance = (funded_chain - spent_chain) + (funded_mempool - spent_mempool)
                tem_saldo = balance > 0
                break  # sucesso -> sai do loop de retries

            elif response.status_code == 429:
                # Rate limit: backoff exponencial com jitter
                backoff = base_backoff * (2 ** (attempt - 1))
                jitter = random.uniform(0, backoff * 0.3)
                sleep_time = backoff + jitter
                print(f"🔴 429 para {endereco} (attempt {attempt}/{max_retries}). Backoff {sleep_time:.2f}s")
                time.sleep(sleep_time)
                continue

            elif 500 <= response.status_code < 600:
                # Erro de servidor temporário -> backoff e retry
                backoff = base_backoff * (2 ** (attempt - 1))
                jitter = random.uniform(0, backoff * 0.2)
                sleep_time = backoff + jitter
                print(f"⚠️ {response.status_code} servidor para {endereco} (attempt {attempt}/{max_retries}). Retentando em {sleep_time:.2f}s")
                time.sleep(sleep_time)
                continue

            else:
                # Outros códigos HTTP (4xx que não são 429 etc.) -> não faz retry extensivo
                print(f"⚠️ Código HTTP {response.status_code} para {endereco} (sem retry).")
                break

        except requests.exceptions.RequestException as e:
            # Erros de rede: retry com backoff
            backoff = base_backoff * (2 ** (attempt - 1))
            jitter = random.uniform(0, backoff * 0.25)
            sleep_time = backoff + jitter
            print(f"⛔ Erro de requisição para {endereco}: {e} (attempt {attempt}/{max_retries}). Retentando em {sleep_time:.2f}s")
            time.sleep(sleep_time)
            continue
        except Exception as e:
            # Erro inesperado: log e não tenta infinitamente
            print(f"⛔ Erro inesperado ao verificar {endereco}: {e}")
            break

    # Assegura o delay anti-bloqueio por thread (mantém comportamento original)
    try:
        time.sleep(API_DELAY_SEC)
    except Exception:
        pass

    # Fecha a session para liberar recursos (não obrigatório, mas limpo)
    try:
        session.close()
    except Exception:
        pass

    return {
        "tem_saldo": tem_saldo,
        "mnemonic": mnemonic,
        "palavra_base": palavra_base,
        "palavra_completa1": palavra_completa1,
        "palavra_completa2": palavra_completa2,
        "info": info
    }

# -------- Signal handling para shutdown seguro -------------
_shutdown_requested = False
def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print("\n🟡 Sinal de interrupção recebido — finalizando com segurança...")

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# -------- Função principal (Loop) ----------------------------------
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
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa1, ultima_completa2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        print("Nenhum checkpoint encontrado, começando do início...\n")
        base_idx, completa_idx = 0, 0

    print(f"Padrão: 10 palavras repetidas + 2 variáveis (j, j+1)")
    print(f"Continuando de '{palavras[base_idx]}' (base), iniciando variação #{completa_idx+1}")
    print(f"Utilizando {MAX_WORKERS_IO} threads para consultas online (I/O) de forma segura.")
    print("\nIniciando geração de combinações BIP39...\n")

    validator = Bip39MnemonicValidator()
    t0 = perf_counter()
    ultimo_salvamento_tempo = time.time()
    last_save_ultimo_time = time.time()

    stats_validas = contador_validas
    stats_saldos = carteiras_com_saldo

    # Inicializa o pool de threads para I/O (agora é ThreadPoolExecutor)
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_IO)
    pending_futures = []

    try:
        for i in range(base_idx, len(palavras)):
            if _shutdown_requested:
                break
            palavra_base = palavras[i]
            base_prefix = " ".join([palavra_base] * 10)
            start_j = completa_idx if i == base_idx else 0

            # O loop vai até len(palavras) - 1 porque a segunda palavra é j + 1
            for j in range(start_j, len(palavras) - 1): 
                if _shutdown_requested:
                    break

                palavra_completa1 = palavras[j]
                palavra_completa2 = palavras[j + 1]
                contador_total += 1

                mnemonic = f"{base_prefix} {palavra_completa1} {palavra_completa2}"

                now = time.time()
                # Salva o último ponto de forma frequente e segura
                if (now - last_save_ultimo_time) >= SAVE_ULTIMO_INTERVAL:
                    salvar_ultima_combinacao(ULTIMO_FILE, palavra_base, palavra_completa1, palavra_completa2)
                    last_save_ultimo_time = now

                # Salva checkpoint de estatísticas
                if now - ultimo_salvamento_tempo > SAVE_INTERVAL_SEC or contador_total % FREQUENCY_SAVE_CHECKPOINT == 0:
                    salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats_validas, stats_saldos)
                    ultimo_salvamento_tempo = now

                # Exibir progresso
                if contador_total % FREQUENCY_PRINT == 0:
                    elapsed = perf_counter() - t0
                    rate = contador_total / elapsed if elapsed > 0 else 0.0
                    print(f"Testadas {contador_total} combinações | Última: {mnemonic}")
                    print(f"  Válidas (processadas): {stats_validas} | Com saldo: {stats_saldos} | Taxa: {rate:.2f} combos/s")

                try:
                    is_valid = validator.IsValid(mnemonic)
                except Exception:
                    is_valid = False

                if is_valid:
                    # 1. Derivação de chaves (CPU) -> Rápido, na thread principal
                    info = derivar_chaves(mnemonic)
                    
                    # 2. Submissão da consulta de saldo (I/O + Delay) -> Para o pool de threads
                    fut = executor.submit(
                        verificar_saldo_api_worker, 
                        mnemonic, palavra_base, palavra_completa1, palavra_completa2, info
                    )
                    pending_futures.append(fut)

                # Processa os resultados prontos do pool de threads
                futures_to_remove = []
                for future in pending_futures:
                    if future.done():
                        try:
                            r = future.result()
                            stats_validas += 1 # Contagem de válidas aumenta ao receber o resultado
                            
                            if r.get("tem_saldo"):
                                stats_saldos += 1
                                salvar_carteira_com_saldo(r["palavra_base"], r["palavra_completa1"], r["palavra_completa2"], r["mnemonic"], r["info"])
                                
                        except Exception as e:
                            print(f"Erro ao processar resultado de thread: {e}")
                        
                        futures_to_remove.append(future)
                
                # Remove os futuros processados
                for future in futures_to_remove:
                    try:
                        pending_futures.remove(future)
                    except ValueError:
                        pass # Já foi removido

            # Resetar índice da palavra completa após processar a primeira palavra base
            completa_idx = 0
            
            # Salvar checkpoint ao fim de cada palavra base
            salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats_validas, stats_saldos)
            print(f"\nConcluído para '{palavra_base}': {stats_validas} válidas processadas, {stats_saldos} com saldo\n")

    except Exception as e:
        print(f"[main] Erro inesperado: {e}")

    finally:
        print("🟢 Aguardando finalização das tasks pendentes do pool...")
        # Processa todos os resultados finais antes de desligar o pool
        for f in as_completed(pending_futures):
            try:
                r = f.result()
                stats_validas += 1
                if r.get("tem_saldo"):
                    stats_saldos += 1
                    salvar_carteira_com_saldo(r["palavra_base"], r["palavra_completa1"], r["palavra_completa2"], r["mnemonic"], r["info"])
            except Exception:
                pass
        
        executor.shutdown(wait=True)

        # Salva o último ponto de parada
        salvar_ultima_combinacao(ULTIMO_FILE, palavra_base if 'palavra_base' in locals() else "", palavra_completa1 if 'palavra_completa1' in locals() else "", palavra_completa2 if 'palavra_completa2' in locals() else "")
        
        # Salvar estatísticas finais
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
