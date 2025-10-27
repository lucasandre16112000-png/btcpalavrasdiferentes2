# -*- coding: utf-8 -*-
import hashlib
import hmac
import requests
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from mnemonic import Mnemonic
from bitcoin import privtopub, pubtoaddr, encode_privkey, encode_pubkey
from bitcoin.wallet import CBitcoinSecret

# ==============================================================================
# CONFIGURAÇÕES GLOBAIS DE RATE LIMIT E CONCORRÊNCIA
# ==============================================================================

# O LIMITADOR GLOBAL DE VELOCIDADE:
# ESSA é a chave para evitar o erro 429. Garante um atraso mínimo entre CADA requisição
# de API, EM TODAS as threads.
# Começamos em 1.0s para máxima estabilidade, mesmo com mais threads.
REQUEST_MIN_DELAY = 1.0 # Segundos (1.0 = 1 requisição por segundo).

# AUMENTAMOS AS THREADS PARA MELHORAR A FILA E O PROCESSAMENTO LOCAL
# 5 threads é um bom ponto de partida para a maioria dos processadores.
MAX_CONCURRENCY_WORKERS = 5 # Aumentado de 3 para 5.

# Defina a palavra base (Ex: 'abandon abandon...').
PALAVRA_BASE_PADRAO = "abandon"

# Configuração de Estabilidade (Retry Logic)
MAX_RETRIES = 7
BASE_BACKOFF_DELAY = 4

# Endereços das APIs para verificação de saldo
API_URLS = [
    "https://api.blockcypher.com/v1/btc/main/addrs/{}"
]
MEMPOOL_API_URL = "https://mempool.space/api/address/{}"

# Dicionário BIP39
WORDLIST = Mnemonic('english').wordlist

# Arquivos de progresso e saldo
CHECKPOINT_FILE = "checkpoint_10+2_SIMPLIFICADO.txt"
SALDO_FILE = "saldo.txt"

# ==============================================================================
# VARIÁVEIS GLOBAIS DE CONTROLE DE THREADS (CRÍTICAS PARA O RATE LIMITER)
# ==============================================================================
rate_limit_lock = threading.Lock()
last_request_time = 0.0            

# Contadores
VALID_BIP39_COUNT = 0
FOUND_WITH_BALANCE = 0
TOTAL_TESTS = 0

# ==============================================================================
# FUNÇÕES DE ESTABILIDADE E UTILIDADE (Mantidas as suas originais)
# ==============================================================================

def save_checkpoint(base_word, var1_idx, var2_idx):
    """Salva o progresso atual no arquivo de checkpoint."""
    with rate_limit_lock:
        try:
            with open(CHECKPOINT_FILE, 'w') as f:
                f.write(f"{base_word}\n{var1_idx}\n{var2_idx}\n{time.time()}")
        except IOError as e:
            print(f"❌ ERRO ao salvar checkpoint: {e}")

def load_checkpoint():
    """Carrega o último progresso salvo ou retorna valores padrão."""
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            lines = [line.strip() for line in f.readlines()]
            if len(lines) >= 3:
                base_word = lines[0]
                var1_idx = int(lines[1])
                var2_idx = int(lines[2])
                return base_word, var1_idx, var2_idx
            else:
                return PALAVRA_BASE_PADRAO, 0, 0
    except (IOError, ValueError):
        return PALAVRA_BASE_PADRAO, 0, 0

def save_to_saldo_file(mnemonic, address, wif, hex_key, pub_key, balance):
    """Salva as informações da carteira com saldo no arquivo 'saldo.txt'."""
    try:
        with open(SALDO_FILE, 'a') as f:
            f.write("=" * 80 + "\n")
            f.write("💎 CARTEIRA COM SALDO ENCONTRADA\n")
            f.write(f"Mnemonic: {mnemonic}\n")
            f.write(f"Endereço: {address}\n")
            f.write(f"Saldo: {balance} BTC\n")
            f.write(f"Chave Privada (WIF): {wif}\n")
            f.write(f"Chave Privada (HEX): {hex_key}\n")
            f.write(f"Chave Pública: {pub_key}\n")
            f.write("-" * 80 + "\n\n")
    except IOError as e:
        print(f"❌ ERRO ao salvar no arquivo 'saldo.txt': {e}")


# ==============================================================================
# FUNÇÕES CRÍTICAS DE CRIAÇÃO E VERIFICAÇÃO DE CHAVES
# ==============================================================================

def generate_key_data(mnemonic):
    """Gera dados de chave (seed, WIF, HEX, Endereço P2PKH) a partir do mnemonic."""
    try:
        seed = Mnemonic.to_seed(mnemonic, passphrase="")
        I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_priv_key = I[:32]
        wif = encode_privkey(master_priv_key, 'wif_compressed')
        pub_key = privtopub(master_priv_key)
        address = pubtoaddr(pub_key)
        hex_key = master_priv_key.hex()
        return address, wif, hex_key, pub_key
      
    except Exception:
        return None, None, None, None

def check_wallet_balance(mnemonic):
    """
    Verifica o saldo do endereço usando múltiplas APIs com Rate Limiter e Backoff.
    """
    global VALID_BIP39_COUNT
    global FOUND_WITH_BALANCE
    global last_request_time
    global PALAVRA_BASE

    if not Mnemonic('english').check(mnemonic):
        return False, 0
  
    address, wif, hex_key, pub_key = generate_key_data(mnemonic)
    if not address:
        return False, 0
  
    with rate_limit_lock:
        global VALID_BIP39_COUNT
        VALID_BIP39_COUNT += 1

    apis_to_check = API_URLS + [MEMPOOL_API_URL]
    balance_btc = 0.0

    for api_url in apis_to_check:
        api_url_base = api_url.split('/api/')[0].split('/v1/')[0]

        for attempt in range(MAX_RETRIES):
            # NOVO: TRAVA DE RATE LIMIT GLOBAL AQUI!
            with rate_limit_lock:
                elapsed = time.time() - last_request_time
                time_to_wait = REQUEST_MIN_DELAY - elapsed
               
                if time_to_wait > 0:
                    time.sleep(time_to_wait)
               
                last_request_time = time.time()

            try:
                # API Call
                url = api_url.format(address)
                response = requests.get(url, timeout=10)
                response.raise_for_status()

                data = response.json()
                balance_satoshi = 0
              
                # Lógica de extração do saldo
                if "blockcypher.com" in api_url:
                    balance_satoshi = data.get('balance', 0)
                elif "mempool.space" in api_url:
                    chain_stats = data.get('chain_stats', {})
                    if chain_stats.get('tx_count', 0) > 0:
                        if chain_stats.get('funded_txo_sum', 0) > 0:
                            balance_satoshi = chain_stats.get('funded_txo_sum', 0)
                        elif data.get('balance', 0) > 0:
                            balance_satoshi = data.get('balance', 0)
              
                balance_btc = balance_satoshi / 100000000.0 if balance_satoshi else 0.0

                if balance_btc > 0:
                    with rate_limit_lock:
                        global FOUND_WITH_BALANCE
                        FOUND_WITH_BALANCE += 1
                  
                    # Log e salvamento
                    details = (
                        "\n" + "=" * 80 +
                        "\n💎 CARTEIRA COM SALDO ENCONTRADA - DETALHES COMPLETOS 💎" +
                        f"\nPalavra Base: {PALAVRA_BASE} (repetida 10x)" +
                        f"\nPalavra 11: {mnemonic.split()[10]}" +
                        f"\nPalavra 12: {mnemonic.split()[11]}" +
                        f"\nMnemonic: {mnemonic}" +
                        f"\nEndereço: {address}" +
                        f"\nChave Privada (WIF): {wif}" +
                        f"\nChave Privada (HEX): {hex_key}" +
                        f"\nChave Pública: {pub_key}" +
                        f"\nSaldo: {balance_btc:.8f} BTC (API: {api_url_base})" +
                        "\n" + "-" * 80 + "\n"
                    )
                    print(details)
                    save_to_saldo_file(mnemonic, address, wif, hex_key, pub_key, balance_btc)

                    return True, balance_btc

                return False, 0
          
            except requests.exceptions.HTTPError as e:
                # LÓGICA DE BACKOFF SE O RATE LIMITER GLOBAL FALHAR (principalmente 429)
                if response.status_code == 429:
                    if attempt < MAX_RETRIES - 1:
                        sleep_time = random.uniform(BASE_BACKOFF_DELAY, BASE_BACKOFF_DELAY * 1.5) * (2 ** attempt)
                        print(f"🟡 AVISO (429 em {api_url_base}): Rate Limiter falhou. Backoff ativado. Tentando em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                        time.sleep(sleep_time)
                    else:
                        print(f"🟠 AVISO: {api_url_base} falhou após {MAX_RETRIES} tentativas. Desistindo desta chave (API).")
                        break
                elif response.status_code == 404:
                    return False, 0
                else:
                    print(f"❌ ERRO HTTP inesperado em {api_url_base}: {response.status_code} - {e}")
                    break
          
            except requests.exceptions.RequestException as e:
                if attempt < MAX_RETRIES - 1:
                    sleep_time = random.uniform(2, 4) * (2 ** attempt)
                    print(f"❌ ERRO de Conexão em {api_url_base}. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                    time.sleep(sleep_time)
                else:
                    print(f"🟠 AVISO: {api_url_base} falhou após {MAX_RETRIES} tentativas. Desistindo desta chave (Conexão).")
                    break

            except Exception as e:
                print(f"❌ ERRO Inesperado ao verificar saldo (API: {api_url_base}): {e}")
                break

    return False, 0

# ==============================================================================
# FUNÇÃO PRINCIPAL DE VARREDURA
# ==============================================================================

def main():
    """Função principal que gerencia a varredura e a concorrência."""
    global PALAVRA_BASE
    global VALID_BIP39_COUNT
    global FOUND_WITH_BALANCE
    global TOTAL_TESTS
    global rate_limit_lock

    start_time = time.monotonic()
   
    PALAVRA_BASE, start_var1_idx, start_var2_idx = load_checkpoint()

    try:
        base_idx = WORDLIST.index(PALAVRA_BASE)
    except ValueError:
        print(f"❌ ERRO: Palavra base '{PALAVRA_BASE}' não encontrada na lista BIP39.")
        return

    print("=" * 80)
    print("Iniciando realfindbitcoin_10+2_SIMPLIFICADO.py - MODO ALTA ESTABILIDADE E ORDEM (10+2)...")
    print("\nConfigurações de Concorrência e Estabilidade:")
    print(f"Limite de concorrência (Threads): {MAX_CONCURRENCY_WORKERS}")
    print(f"🛑 RATE LIMIT GLOBAL FIXO: {REQUEST_MIN_DELAY}s (Chave para evitar o 429)")
    print(f"Tentativas de API (Max Retries): {MAX_RETRIES}")
    print(f"Atraso Base (Backoff Delay): {BASE_BACKOFF_DELAY}s")
    print("-" * 80)
    print(f"Continuando da Base: '{PALAVRA_BASE}' | Variável 11 (Idx): {start_var1_idx} | Variável 12 (Idx): {start_var2_idx}")
    print("=" * 80)
    print("Pressione Ctrl+C para parar com segurança.\n")

    futures = []

    try:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY_WORKERS) as executor:
            for i in range(base_idx, len(WORDLIST)):
                PALAVRA_BASE = WORDLIST[i]
              
                if i > base_idx:
                    start_var1_idx = 0

                for j in range(start_var1_idx, len(WORDLIST)):
                    var1_word = WORDLIST[j]

                    if j > start_var1_idx:
                        start_var2_idx = 0

                    for k in range(start_var2_idx, len(WORDLIST)):
                        var2_word = WORDLIST[k]

                        mnemonic = f"{PALAVRA_BASE} " * 10 + f"{var1_word} {var2_word}"
                      
                        future = executor.submit(check_wallet_balance, mnemonic)
                        futures.append(future)
                      
                        TOTAL_TESTS += 1

                        # Gerenciamento de Futures (processa o que terminou para manter o buffer)
                        if len(futures) >= MAX_CONCURRENCY_WORKERS * 2:
                            completed_futures = as_completed(futures)
                           
                            for completed_future in completed_futures:
                                try:
                                    completed_future.result()
                                except Exception:
                                    pass
                                futures.remove(completed_future)
                                break

                        # Atualização de status a cada 100 chaves testadas
                        if TOTAL_TESTS % 100 == 0:
                            last_mnemonic_words = mnemonic.split()[-3:]
                            print(f"Testadas {TOTAL_TESTS} combinações | Última: {last_mnemonic_words[0]} {last_mnemonic_words[1]} {last_mnemonic_words[2]}" +
                                  f" Válidas (BIP39): {VALID_BIP39_COUNT} | Com saldo: {FOUND_WITH_BALANCE}")
                          
                            save_checkpoint(PALAVRA_BASE, j, k + 1)
                          
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass

    except KeyboardInterrupt:
        print("\n\n🛑 PARADA SOLICITADA PELO USUÁRIO (Ctrl+C). Salvando progresso...")
        save_checkpoint(PALAVRA_BASE, j, k)
        print("✅ Progresso salvo com sucesso.")
        print("Programa encerrado.")
        return
  
    except Exception as e:
        print(f"\n\n❌ ERRO FATAL: {e}. Salvando último progresso conhecido.")
        save_checkpoint(PALAVRA_BASE, j, k)
        return

    end_time = time.monotonic()
    print("\n" + "="*50)
    print("✨ Processo Concluído ✨")
    print(f"Tempo total decorrido: {end_time - start_time:.2f} segundos")
    print(f"Combinações testadas: {TOTAL_TESTS}")
    print("="*50)

if __name__ == '__main__':
    main()
