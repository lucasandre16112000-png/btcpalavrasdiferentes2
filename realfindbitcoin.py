# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import time
import random
from typing import Dict, Any, Tuple
import hashlib
import hmac
from mnemonic import Mnemonic

# Importações originais (mantidas por fidelidade ao código original)
from bitcoin import privtopub, pubtoaddr, encode_privkey
from bitcoin.wallet import CBitcoinSecret

# ==============================================================================
# CONFIGURAÇÕES GLOBAIS
# ==============================================================================

# Defina a palavra base que se repetirá 10 vezes.
PALAVRA_BASE_PADRAO = "abandon"

# Configurações de Concorrência Otimizada (ULTRA-ESTÁVEL)
# Reduzimos o limite para **2** consultas simultâneas. Este valor ultraconservador
# é necessário para sobreviver ao Rate Limit inicial da API do BlockCypher (429).
MAX_CONCURRENT_REQUESTS = 2 

# Configuração de Estabilidade (Melhorias contra 429)
MAX_RETRIES = 7 
# A base do backoff exponencial. Aumentamos para 6 segundos para dar mais folga.
# O atraso é **NÃO-BLOQUEANTE**, garantindo que o programa continue o trabalho útil.
BASE_BACKOFF_DELAY = 6 

# Endereços das APIs para verificação de saldo (mantidos originais)
API_URLS = [
    "https://api.blockcypher.com/v1/btc/main/addrs/{}"
]
MEMPOOL_API_URL = "https://mempool.space/api/address/{}"

# Dicionário BIP39 em inglês
WORDLIST = Mnemonic('english').wordlist

# Arquivos para salvar progresso e resultados
CHECKPOINT_FILE = "checkpoint_10+2_SIMPLIFICADO.txt"
SALDO_FILE = "saldo.txt"

# Variáveis globais de contagem (serão atualizadas de forma segura)
VALID_BIP39_COUNT = 0
FOUND_WITH_BALANCE = 0
TOTAL_TESTS = 0
PALAVRA_BASE = PALAVRA_BASE_PADRAO


# ==============================================================================
# FUNÇÕES DE ESTABILIDADE E UTILIDADE (Mantidas Síncronas)
# ==============================================================================

def save_checkpoint(base_word: str, var1_idx: int, var2_idx: int):
    """Salva o progresso atual no arquivo de checkpoint."""
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            f.write(f"{base_word}\n{var1_idx}\n{var2_idx}\n{time.time()}")
    except IOError as e:
        print(f"❌ ERRO ao salvar checkpoint: {e}")

def load_checkpoint() -> Tuple[str, int, int]:
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

def save_to_saldo_file(mnemonic: str, address: str, wif: str, hex_key: str, pub_key: str, balance: float):
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
# FUNÇÕES CRÍTICAS DE CRIAÇÃO E VERIFICAÇÃO DE CHAVES (ASSÍNCRONAS)
# ==============================================================================

def generate_key_data(mnemonic: str) -> Tuple[str, str, str, str] | Tuple[None, None, None, None]:
    """
    Gera dados de chave (WIF, HEX, Endereço P2PKH) a partir do mnemonic.
    (Sua lógica original de derivação de chaves foi preservada 100%)
    """
    try:
        # A Mnemonic BIP39 de 12 palavras gera uma semente de 128 bits (16 bytes)
        seed = Mnemonic.to_seed(mnemonic, passphrase="")
       
        # A chave privada principal (Master Private Key)
        I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_priv_key = I[:32]
       
        # Convertendo a chave mestre para WIF (formato compactado/comprimido)
        wif = encode_privkey(master_priv_key, 'wif_compressed')
       
        # Gerando a chave pública comprimida
        pub_key = privtopub(master_priv_key)
       
        # Endereço P2PKH (o mais comum, que começa com '1')
        address = pubtoaddr(pub_key)
       
        # Chave privada em HEX (sem compressão/prefixo/checksum)
        hex_key = master_priv_key.hex()
        
        return address, wif, hex_key, pub_key
    except Exception:
        return None, None, None, None


async def check_wallet_balance(
    session: aiohttp.ClientSession, 
    mnemonic: str, 
    semaphore: asyncio.Semaphore
) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Verifica o saldo de forma assíncrona, com limite de concorrência e backoff NÃO-BLOQUEANTE.
    """
    # 1. Verificar se o mnemonic é válido (Checksum)
    if not Mnemonic('english').check(mnemonic):
        return False, 0, {"status": "INVALID_BIP39"}
   
    # Gerar dados da chave
    address, wif, hex_key, pub_key = generate_key_data(mnemonic)
    if not address:
        return False, 0, {"status": "KEY_GEN_ERROR"}

    # 2. Se a chave for BIP39 válida, incrementamos o contador
    global VALID_BIP39_COUNT
    VALID_BIP39_COUNT += 1

    # 3. Verificar o saldo através das APIs
    apis_to_check = API_URLS + [MEMPOOL_API_URL]

    # O semáforo limita o número máximo de requisições ativas (PREVINE 429)
    async with semaphore:
        for api_url in apis_to_check:
            api_url_base = api_url.split('/api/')[0].split('/v1/')[0].replace("https://", "")
            
            for attempt in range(MAX_RETRIES):
                try:
                    url = api_url.format(address)
                    
                    # Usa aiohttp para requisição assíncrona com timeout
                    async with session.get(url, timeout=10) as response:
                        
                        # --- Tratamento de Rate Limit (429) ---
                        if response.status == 429:
                            # Backoff Exponencial Aprimorado
                            sleep_time = random.uniform(BASE_BACKOFF_DELAY, BASE_BACKOFF_DELAY * 1.5) * (2 ** attempt)
                            print(f"[{address[:6]}...] 🟡 AVISO (Instabilidade - 429 em {api_url_base}): Backoff ativado. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                            # AQUI: O await asyncio.sleep() é NÃO-BLOQUEANTE, garantindo estabilidade.
                            await asyncio.sleep(sleep_time)
                            continue  # Tenta novamente
                        
                        # --- Sucesso (200) ou Não Encontrado (404) ---
                        elif response.status == 200:
                            data = await response.json()
                            balance_satoshi = 0

                            # Lógica de extração de saldo (Mantida original)
                            if "blockcypher.com" in api_url:
                                balance_satoshi = data.get('balance', 0)
                            elif "mempool.space" in api_url:
                                chain_stats = data.get('chain_stats', {})
                                if chain_stats.get('funded_txo_sum', 0) > 0:
                                    balance_satoshi = chain_stats.get('funded_txo_sum', 0)
                                elif data.get('balance', 0) > 0:
                                    balance_satoshi = data.get('balance', 0)
                            
                            balance_btc = balance_satoshi / 100000000.0 if balance_satoshi else 0.0

                            if balance_btc > 0:
                                # Retorna sucesso e os detalhes para salvamento
                                details = {
                                    "address": address, "wif": wif, "hex_key": hex_key, 
                                    "pub_key": pub_key, "api_base": api_url_base,
                                    "mnemonic": mnemonic
                                }
                                return True, balance_btc, details

                            # Saldo zero, mas a chave é BIP39 válida
                            return False, 0, {"status": "ZERO_BALANCE"}
                        
                        elif response.status == 404:
                            # 404 geralmente significa que a API não encontrou transações/dados (saldo zero)
                            return False, 0, {"status": "ZERO_BALANCE_404"}
                        
                        # --- Outros Erros HTTP ---
                        else:
                            print(f"[{address[:6]}...] ❌ ERRO HTTP inesperado em {api_url_base}: {response.status} - Parando.")
                            break # Tenta próxima API

                except aiohttp.ClientError as e:
                    # Trata erros de conexão ou timeout
                    if attempt < MAX_RETRIES - 1:
                        sleep_time = random.uniform(2, 4) * (2 ** attempt)
                        print(f"[{address[:6]}...] ❌ ERRO de Conexão em {api_url_base}. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                        await asyncio.sleep(sleep_time)
                    else:
                        print(f"[{address[:6]}...] 🟠 AVISO (Estabilidade Máxima): {api_url_base} falhou após {MAX_RETRIES} tentativas. Desistindo desta chave (Conexão).")
                        break # Tenta próxima API
                
                except asyncio.TimeoutError:
                    if attempt < MAX_RETRIES - 1:
                        sleep_time = random.uniform(2, 4) * (2 ** attempt)
                        print(f"[{address[:6]}...] 🔴 ERRO: Timeout assíncrono em {api_url_base}. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                        await asyncio.sleep(sleep_time)
                    else:
                        print(f"[{address[:6]}...] 🟠 AVISO (Estabilidade Máxima): {api_url_base} falhou por Timeout após {MAX_RETRIES} tentativas. Desistindo desta chave (Timeout).")
                        break # Tenta próxima API
                
                except Exception as e:
                    print(f"[{address[:6]}...] ❌ ERRO Inesperado ao verificar saldo (API: {api_url_base}): {e}")
                    break # Tenta próxima API

    # Se todas as APIs falharem após todas as retentativas, retorna falha
    return False, 0, {"status": "ALL_APIS_FAILED"}


# ==============================================================================
# FUNÇÃO PRINCIPAL DE VARREDURA
# ==============================================================================

async def run_scanner():
    """Função principal que gerencia a varredura e a concorrência assíncrona."""
    global PALAVRA_BASE, VALID_BIP39_COUNT, FOUND_WITH_BALANCE, TOTAL_TESTS

    start_time = time.monotonic()
   
    # Carrega o ponto de parada (Sua lógica de carregamento é preservada)
    PALAVRA_BASE, start_var1_idx, start_var2_idx = load_checkpoint()

    try:
        base_idx = WORDLIST.index(PALAVRA_BASE)
    except ValueError:
        print(f"❌ ERRO: Palavra base '{PALAVRA_BASE}' não encontrada na lista BIP39.")
        return

    print("=" * 80)
    print(f"Iniciando varredura ASSÍNCRONA - MODO LÓGICA 10+2 | ULTRA-ESTÁVEL")
    print("\nConfigurações de Concorrência e Estabilidade (MODO CONSERVADOR):")
    print(f"Limite de requisições simultâneas (Semáforo): {MAX_CONCURRENT_REQUESTS} (Ultra-Conservador)")
    print(f"Tentativas de API (Max Retries): {MAX_RETRIES}")
    print(f"Atraso Base (Backoff Delay): {BASE_BACKOFF_DELAY}s (NÃO-BLOQUEANTE)")
    print("-" * 80)
    print(f"Continuando da Base: '{PALAVRA_BASE}' | Variável 11 (Idx): {start_var1_idx} | Variável 12 (Idx): {start_var2_idx}")
    print("=" * 80)
    print("Pressione Ctrl+C para parar com segurança.\n")
    
    # Cria o Semáforo e a lista de tarefas
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    tasks = []
    
    # Variáveis de controle de loop em caso de interrupção (necessário para o checkpoint)
    i, j, k = base_idx, start_var1_idx, start_var2_idx
    
    try:
        # Cria uma sessão aiohttp para todas as requisições (mais eficiente)
        async with aiohttp.ClientSession() as session:
            
            # Loop da Palavra Base (1ª a 10ª palavra) - Lógica Preservada
            for i in range(base_idx, len(WORDLIST)):
                PALAVRA_BASE = WORDLIST[i]
                
                if i > base_idx:
                    start_var1_idx = 0

                # Loop da Variável 11 (11ª palavra) - Lógica Preservada
                for j in range(start_var1_idx, len(WORDLIST)):
                    var1_word = WORDLIST[j]

                    if j > start_var1_idx:
                        start_var2_idx = 0

                    # Loop da Variável 12 (12ª palavra) - Lógica Preservada
                    for k in range(start_var2_idx, len(WORDLIST)):
                        var2_word = WORDLIST[k]

                        # 10 repetições da PALAVRA_BASE + Palavra 11 + Palavra 12
                        mnemonic = f"{PALAVRA_BASE} " * 10 + f"{var1_word} {var2_word}"
                        
                        # Cria uma TAREFA ASSÍNCRONA
                        task = asyncio.create_task(check_wallet_balance(session, mnemonic, semaphore))
                        tasks.append(task)
                        
                        TOTAL_TESTS += 1

                        # Gerenciamento Assíncrono para processar resultados e liberar memória
                        if len(tasks) >= MAX_CONCURRENT_REQUESTS * 2:
                            # Aguarda o primeiro que terminar (para evitar bloquear o loop)
                            done, pending = await asyncio.wait(
                                tasks, 
                                return_when=asyncio.FIRST_COMPLETED,
                                timeout=0.01 
                            )
                            
                            # Processa as tarefas concluídas
                            for completed_task in done:
                                found, balance, details = completed_task.result()
                                if found:
                                    global FOUND_WITH_BALANCE
                                    FOUND_WITH_BALANCE += 1
                                    
                                    # Log e salvamento no arquivo TXT
                                    output = (
                                        "\n" + "=" * 80 +
                                        "\n💎 CARTEIRA COM SALDO ENCONTRADA - DETALHES COMPLETOS 💎" +
                                        f"\nPalavra Base: {PALAVRA_BASE} (repetida 10x)" +
                                        f"\nPalavra 11: {details['mnemonic'].split()[10]}" +
                                        f"\nPalavra 12: {details['mnemonic'].split()[11]}" +
                                        f"\nMnemonic: {details['mnemonic']}" +
                                        f"\nEndereço: {details['address']}" +
                                        f"\nChave Privada (WIF): {details['wif']}" +
                                        f"\nChave Privada (HEX): {details['hex_key']}" +
                                        f"\nChave Pública: {details['pub_key']}" +
                                        f"\nSaldo: {balance:.8f} BTC (API: {details['api_base']})" +
                                        "\n" + "-" * 80 + "\n"
                                    )
                                    print(output)
                                    save_to_saldo_file(details['mnemonic'], details['address'], details['wif'], details['hex_key'], details['pub_key'], balance)
                                
                                tasks.remove(completed_task)
                            
                            # Atualização de status e checkpoint (Lógica Preservada)
                            if TOTAL_TESTS % 100 == 0:
                                last_mnemonic_words = mnemonic.split()[-3:]
                                print(f"Testadas {TOTAL_TESTS} combinações | Última: {last_mnemonic_words[0]} {last_mnemonic_words[1]} {last_mnemonic_words[2]}" +
                                      f" Válidas (BIP39): {VALID_BIP39_COUNT} | Com saldo: {FOUND_WITH_BALANCE}")
                                # O +1 é porque a próxima iteração começa em k+1
                                save_checkpoint(PALAVRA_BASE, j, k + 1)
                            
            # Processa as tarefas remanescentes no final
            if tasks:
                print("\n🟢 Aguardando finalização das tarefas de consulta de saldo pendentes...")
                # asyncio.gather garante que nenhuma tarefa seja esquecida
                results = await asyncio.gather(*tasks)

                for found, balance, details in results:
                    if found:
                        FOUND_WITH_BALANCE += 1
                        # Salvamento de resultados remanescentes
                        output = (
                            "\n" + "=" * 80 +
                            "\n💎 CARTEIRA COM SALDO ENCONTRADA - DETALHES COMPLETOS 💎" +
                            f"\nPalavra Base: {PALAVRA_BASE} (repetida 10x)" +
                            f"\nPalavra 11: {details['mnemonic'].split()[10]}" +
                            f"\nPalavra 12: {details['mnemonic'].split()[11]}" +
                            f"\nMnemonic: {details['mnemonic']}" +
                            f"\nEndereço: {details['address']}" +
                            f"\nChave Privada (WIF): {details['wif']}" +
                            f"\nChave Privada (HEX): {details['hex_key']}" +
                            f"\nChave Pública: {details['pub_key']}" +
                            f"\nSaldo: {balance:.8f} BTC (API: {details['api_base']})" +
                            "\n" + "-" * 80 + "\n"
                        )
                        print(output)
                        save_to_saldo_file(details['mnemonic'], details['address'], details['wif'], details['hex_key'], details['pub_key'], balance)
                        
    except KeyboardInterrupt:
        print("\n\n🛑 PARADA SOLICITADA PELO USUÁRIO (Ctrl+C). Salvando progresso...")
        # Salva a última posição (k) anterior à interrupção
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
    # Inicializa o loop de eventos assíncrono
    asyncio.run(run_scanner())
