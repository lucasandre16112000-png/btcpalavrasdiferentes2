# -*- coding: utf-8 -*-
import hashlib
import hmac
import requests
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from mnemonic import Mnemonic
from bitcoin import privtopub, pubtoaddr, encode_privkey, encode_pubkey
from bitcoin.wallet import CBitcoinSecret

# ==============================================================================
# CONFIGURAÇÕES GLOBAIS
# ==============================================================================

# Defina a palavra base que se repetirá 10 vezes.
# Ex: 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon'
# O script carregará o último ponto de parada (checkpoint) se ele existir.
PALAVRA_BASE_PADRAO = "abandon"

# O limite de concorrência (threads) é o fator que mais afeta o erro 429.
# 1-2: Máxima estabilidade.
# 3-4: Equilíbrio. (Recomendado após o ajuste do backoff)
# 5+: Muito agressivo.
MAX_CONCURRENCY_WORKERS = 3 # Aumentamos para 3. Se ainda der 429, reduza para 2 ou 1.

# Configuração de Estabilidade (Melhorias contra 429)
MAX_RETRIES = 7 # Aumentado de 5 para 7 tentativas
# A base do backoff exponencial foi aumentada para dar mais tempo entre as tentativas.
# Isso reduz o erro 429 no mempool.space.
BASE_BACKOFF_DELAY = 4 # Segundos. Aumentado de 2 para 4.

# Endereços das APIs para verificação de saldo
API_URLS = [
    "https://api.blockcypher.com/v1/btc/main/addrs/{}"
]
# Mempool.space é muito restrito, mantemos como backup apenas.
MEMPOOL_API_URL = "https://mempool.space/api/address/{}"

# Dicionário BIP39 em inglês (já em ordem alfabética)
WORDLIST = Mnemonic('english').wordlist

# Arquivo para salvar o ponto de parada
CHECKPOINT_FILE = "checkpoint_10+2_SIMPLIFICADO.txt"
# Arquivo para salvar chaves com saldo
SALDO_FILE = "saldo.txt"

# ==============================================================================
# FUNÇÕES DE ESTABILIDADE E UTILIDADE
# ==============================================================================

def save_checkpoint(base_word, var1_idx, var2_idx):
    """Salva o progresso atual no arquivo de checkpoint."""
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
        # A Mnemonic BIP39 de 12 palavras gera uma semente de 128 bits (16 bytes)
        # O padrão é usar "Bitcoin seed" como passphrase.
        seed = Mnemonic.to_seed(mnemonic, passphrase="")
       
        # A chave privada principal (Master Private Key)
        # Hmac SHA512 é usado para derivar a Master Private Key
        I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_priv_key = I[:32]
       
        # Convertendo a chave mestre para WIF (formato compactado/comprimido)
        # Usamos 0x80 para mainnet, e adicionamos 0x01 no final para o formato 'compressed'
        wif = encode_privkey(master_priv_key, 'wif_compressed')
       
        # Gerando a chave pública comprimida
        pub_key = privtopub(master_priv_key)
       
        # Endereço P2PKH (o mais comum, que começa com '1')
        address = pubtoaddr(pub_key)
       
        # Chave privada em HEX (sem compressão/prefixo/checksum)
        hex_key = master_priv_key.hex()

        return address, wif, hex_key, pub_key
       
    except Exception as e:
        #print(f"❌ ERRO ao gerar dados da chave: {e}")
        return None, None, None, None

def check_wallet_balance(address, mnemonic):
    """
    Verifica o saldo do endereço usando múltiplas APIs com backoff exponencial.
    Retorna True e o saldo se houver fundos, False caso contrário.
    """
    global VALID_BIP39_COUNT # Necessário para atualizar o contador global
    global FOUND_WITH_BALANCE # Necessário para atualizar o contador global

    # 1. Verificar se o mnemonic é válido (Checksum)
    if not Mnemonic('english').check(mnemonic):
        return False, 0 # Não é uma chave BIP39 válida
   
    # Se for BIP39, geramos os dados da chave
    address, wif, hex_key, pub_key = generate_key_data(mnemonic)
    if not address:
        return False, 0
   
    # 2. Se a chave for BIP39 válida, incrementamos o contador
    VALID_BIP39_COUNT += 1

    # 3. Verificar o saldo através das APIs
    apis_to_check = API_URLS + [MEMPOOL_API_URL] # Inclui o mempool como último recurso

    for api_url in apis_to_check:
        api_url_base = api_url.split('/api/')[0].split('/v1/')[0] # Obtém a base da URL para prints

        for attempt in range(MAX_RETRIES):
            try:
                # API Call
                url = api_url.format(address)
                response = requests.get(url, timeout=10)
                response.raise_for_status()  # Levanta erro para 4xx/5xx

                data = response.json()

                # --- Lógica de extração do saldo ---
                balance_satoshi = 0
               
                if "blockcypher.com" in api_url:
                    balance_satoshi = data.get('balance', 0)
                elif "mempool.space" in api_url:
                    # Mempool usa o campo 'chain_stats' -> 'funded_txo_sum' ou 'balance'
                    # Vamos somar a soma total de inputs e outputs para ser mais seguro,
                    # mas o principal é a verificação de transações.
                    chain_stats = data.get('chain_stats', {})
                    # Se houver transações (tx_count > 0), pode ter saldo.
                    # Mas para o saldo exato, precisamos dos "utxos" (Unspent Transaction Outputs)
                    if chain_stats.get('tx_count', 0) > 0:
                        # Para mempool, sem a lista UTXO, verificamos se há algum 'funded' output
                        # ou, de forma mais simples e robusta, se a contagem de transações é > 0.
                        # Contudo, só verificamos se o saldo é **diferente de zero**.
                        # Blockstream é mais direto para o saldo.
                        # Vamos usar o Blockstream (agora via Mempool) para a verificação mais simples.
                        if chain_stats.get('funded_txo_sum', 0) > 0:
                            balance_satoshi = chain_stats.get('funded_txo_sum', 0)
                        elif data.get('balance', 0) > 0: # Caso a API Mempool mude, tentamos o 'balance' direto.
                            balance_satoshi = data.get('balance', 0)
                    else:
                        balance_satoshi = 0 # Sem transações, saldo zero.
               
                # --- Fim da Lógica de extração do saldo ---

                balance_btc = balance_satoshi / 100000000.0 if balance_satoshi else 0.0

                if balance_btc > 0:
                    FOUND_WITH_BALANCE += 1
                   
                    # Log e salvamento no arquivo TXT
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

                # Saldo zero, mas a chave é BIP39 válida
                return False, 0

            except requests.exceptions.HTTPError as e:
                # Se for 429, ativamos o backoff
                if response.status_code == 429:
                    if attempt < MAX_RETRIES - 1:
                        # Backoff Exponencial Aprimorado
                        sleep_time = random.uniform(BASE_BACKOFF_DELAY, BASE_BACKOFF_DELAY * 1.5) * (2 ** attempt)
                        print(f"🟡 AVISO (Instabilidade - 429 em {api_url_base}): Backoff ativado. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                        time.sleep(sleep_time)
                    else:
                        print(f"🟠 AVISO (Estabilidade Máxima): {api_url_base} falhou após {MAX_RETRIES} tentativas. Desistindo desta chave (API).")
                        break # Próxima API na lista
                elif response.status_code == 404:
                    # 404 geralmente significa que o endereço existe, mas não há dados (saldo zero)
                    return False, 0
                else:
                    # Outros erros HTTP (500, etc.)
                    print(f"❌ ERRO HTTP inesperado em {api_url_base}: {response.status_code} - {e}")
                    break # Próxima API na lista
           
            except requests.exceptions.RequestException as e:
                # Outros erros de conexão ou timeout
                if attempt < MAX_RETRIES - 1:
                    sleep_time = random.uniform(2, 4) * (2 ** attempt)
                    print(f"❌ ERRO de Conexão em {api_url_base}. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                    time.sleep(sleep_time)
                else:
                    print(f"🟠 AVISO (Estabilidade Máxima): {api_url_base} falhou após {MAX_RETRIES} tentativas. Desistindo desta chave (Conexão).")
                    break # Próxima API na lista

            except Exception as e:
                print(f"❌ ERRO Inesperado ao verificar saldo (API: {api_url_base}): {e}")
                break # Próxima API na lista

    # Se todas as APIs falharem, retornamos saldo zero
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

    # Inicialização
    VALID_BIP39_COUNT = 0
    FOUND_WITH_BALANCE = 0
    TOTAL_TESTS = 0
   
    # Carrega o ponto de parada
    PALAVRA_BASE, start_var1_idx, start_var2_idx = load_checkpoint()

    # Define o índice de base a partir da palavra carregada
    try:
        base_idx = WORDLIST.index(PALAVRA_BASE)
    except ValueError:
        print(f"❌ ERRO: Palavra base '{PALAVRA_BASE}' não encontrada na lista BIP39.")
        return

    print("=" * 80)
    print(f"Iniciando realfindbitcoin_10+2_SIMPLIFICADO.py - MODO LÓGICA 10+2 e ALTA VELOCIDADE (Apenas Salvamento TXT)...")
    print("\nConfigurações de Concorrência e Estabilidade:")
    print(f"Limite de concorrência: {MAX_CONCURRENCY_WORKERS}")
    print(f"Tentativas de API (Max Retries): {MAX_RETRIES}")
    print(f"Atraso Base (Backoff Delay): {BASE_BACKOFF_DELAY}s (Aumentado para reduzir 429)")
    print("-" * 80)
    print(f"Continuando da Base: '{PALAVRA_BASE}' | Variável 11 (Idx): {start_var1_idx} | Variável 12 (Idx): {start_var2_idx}")
    print("=" * 80)
    print("Pressione Ctrl+C para parar com segurança.\n")

    # Lista para armazenar as futures (tarefas) enviadas para o thread pool
    futures = []

    try:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY_WORKERS) as executor:
            # Loop da Palavra Base (1ª a 10ª palavra)
            for i in range(base_idx, len(WORDLIST)):
                PALAVRA_BASE = WORDLIST[i]
               
                # Reseta o start_var1_idx se a palavra base for nova
                if i > base_idx:
                    start_var1_idx = 0

                # Loop da Variável 11 (11ª palavra)
                for j in range(start_var1_idx, len(WORDLIST)):
                    var1_word = WORDLIST[j]

                    # Reseta o start_var2_idx se a palavra 11 for nova
                    if j > start_var1_idx:
                        start_var2_idx = 0

                    # Loop da Variável 12 (12ª palavra)
                    for k in range(start_var2_idx, len(WORDLIST)):
                        var2_word = WORDLIST[k]

                        # 10 repetições da PALAVRA_BASE + Palavra 11 + Palavra 12
                        mnemonic = f"{PALAVRA_BASE} " * 10 + f"{var1_word} {var2_word}"
                       
                        # Envia a tarefa para o pool de threads
                        future = executor.submit(check_wallet_balance, None, mnemonic)
                        futures.append(future)
                       
                        TOTAL_TESTS += 1

                        # Aguarda a conclusão de algumas tarefas para liberar slots
                        if len(futures) >= MAX_CONCURRENCY_WORKERS * 2: # Mantém um buffer
                            completed_futures = as_completed(futures)
                            # Processa as que já terminaram para manter o loop rodando
                            for completed_future in completed_futures:
                                try:
                                    completed_future.result() # Apenas para verificar exceções
                                except Exception as e:
                                    # Se a exceção já foi tratada dentro da função, não faz nada
                                    pass
                                futures.remove(completed_future)
                                break # Processa uma e volta para o loop de geração

                        # Atualização de status a cada 100 chaves testadas
                        if TOTAL_TESTS % 100 == 0:
                            last_mnemonic_words = mnemonic.split()[-3:]
                            print(f"Testadas {TOTAL_TESTS} combinações | Última: {last_mnemonic_words[0]} {last_mnemonic_words[1]} {last_mnemonic_words[2]}" +
                                  f" Válidas (BIP39): {VALID_BIP39_COUNT} | Com saldo: {FOUND_WITH_BALANCE}")
                           
                            # Salva o checkpoint a cada 100 testes (para não perder progresso)
                            # Base word (WORDLIST[i]), Indice da 11ª palavra (j), Indice da 12ª palavra (k+1)
                            # O +1 é porque a próxima iteração começa em k+1
                            save_checkpoint(PALAVRA_BASE, j, k + 1)
                           
                            # Pequena pausa para reduzir ainda mais o 429
                            time.sleep(0.05)


            # Processa as futures remanescentes no final
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    pass

    except KeyboardInterrupt:
        print("\n\n🛑 PARADA SOLICITADA PELO USUÁRIO (Ctrl+C). Salvando progresso...")
        # Salva o último ponto de parada
        save_checkpoint(PALAVRA_BASE, j, k) # Salva a posição anterior à interrupção
        print("✅ Progresso salvo com sucesso.")
        print("Programa encerrado.")
        return
   
    except Exception as e:
        print(f"\n\n❌ ERRO FATAL: {e}. Salvando último progresso conhecido.")
        save_checkpoint(PALAVRA_BASE, j, k)
        return

if __name__ == '__main__':
    main()
