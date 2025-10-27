#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""
realfindbitcoin.py (versão com Assincronicidade AIOHTTP e Estabilidade Aprimorada)

- Utiliza 'asyncio' e 'aiohttp' para I/O não-bloqueante.
- O foco é na ESTABILIDADE: Redução da concorrência e implementação de um sistema
  robusto de retentativas com backoff exponencial + jitter (aleatoriedade) para
  prevenir bloqueios (429) e falhas de conexão.
"""

import os
import time
import json
import asyncio
import random
import aiohttp 
import threading
from concurrent.futures import ThreadPoolExecutor
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes

# ------------------------
# CONFIGURAÇÃO RÁPIDA
# ------------------------
CHECKPOINT_FILE = "checkpoint.txt"
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"

FREQUENCY_PRINT = 10        # print a cada N combinações
FREQUENCY_SAVE = 10         # salvar checkpoint a cada N combinações
SAVE_INTERVAL_SEC = 15      # ou salvar a cada X segundos (tempo)
CONCURRENCY_LIMIT = 5       # <<<< AJUSTE DE ESTABILIDADE: Reduzido de 8 para 5.
MAX_API_RETRIES = 5         # Número máximo de vezes para tentar a consulta de saldo

# Lista de APIs de exploradores de blockchain
EXPLORER_APIS = [
    "https://mempool.space/api/address/",
    "https://blockstream.info/api/address/",
    "https://api.blockcypher.com/v1/btc/main/addrs/"
]

# locks para controle de concorrência (ainda necessários para I/O de disco)
_stats_lock = threading.Lock()
_file_lock = threading.Lock()

# ------------------------
# Helpers de arquivo atômicos (mantidos síncronos, pois são I/O de disco)
# ------------------------
def atomic_write(path: str, content: str, encoding='utf-8'):
    """Escreve o conteúdo de forma segura (atômica) no arquivo."""
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
    """Adiciona texto ao arquivo e força a sincronização (seguro contra falhas)."""
    with open(path, "a", encoding=encoding) as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

# ------------------------
# Funções de I/O e estado (Retidas do script anterior)
# ------------------------

def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    """Carrega as palavras BIP39 do arquivo."""
    if not os.path.exists(arquivo):
        raise FileNotFoundError(f"Arquivo {arquivo} não encontrado! Crie este arquivo com as 2048 palavras BIP39, uma por linha.")
    with open(arquivo, 'r', encoding='utf-8') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    if len(palavras) != 2048:
        print(f"Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    return palavras


def carregar_ultima_combinacao(arquivo=ULTIMO_FILE):
    """Carrega o último mnemonic testado para continuar o trabalho."""
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
    """Carrega as estatísticas do arquivo de checkpoint."""
    contador_total = contador_validas = carteiras_com_saldo = 0
    if not os.path.exists(arquivo):
        salvar_checkpoint(arquivo, 0, "", 0, 0, 0)
        return contador_total, contador_validas, carteiras_com_saldo
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            for line in f:
                if "Total de combinações testadas:" in line:
                    try:
                        contador_total = int(line.split(":")[1].strip())
                    except:
                        contador_total = 0
                elif "Combinações válidas:" in line:
                    try:
                        contador_validas = int(line.split(":")[1].strip())
                    except:
                        contador_validas = 0
                elif "Carteiras com saldo:" in line:
                    try:
                        carteiras_com_saldo = int(line.split(":")[1].strip())
                    except:
                        carteiras_com_saldo = 0
    except Exception as e:
        print(f"Erro ao ler checkpoint: {e}")
    return contador_total, contador_validas, carteiras_com_saldo


def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2):
    """Encontra o próximo índice de onde continuar a varredura."""
    try:
        base_idx = palavras.index(ultima_base)
        completa2_idx = palavras.index(ultima_completa2)
        
        # Se a próxima palavra completa2 está dentro do limite
        if completa2_idx + 1 < len(palavras):
            return base_idx, completa2_idx 
        
        # Se a palavra base pode ser incrementada
        if base_idx + 1 < len(palavras):
            return base_idx + 1, 0
        else:
            return None, None # Fim da varredura
        
    except ValueError:
        return 0, 0


def salvar_ultima_combinacao(arquivo=ULTIMO_FILE, palavra_base="", palavra_completa1="", palavra_completa2=""):
    """Salva a última combinação testada de forma atômica."""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    mnemonic = " ".join(palavras)
    with _file_lock:
        atomic_write(arquivo, mnemonic)


def salvar_checkpoint(arquivo=CHECKPOINT_FILE, base_idx=0, palavra_base="", contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    """Salva o progresso e estatísticas de forma atômica."""
    texto = (
        f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n"
        f"Total de combinações testadas: {contador_total}\n"
        f"Combinações válidas: {contador_validas}\n"
        f"Carteiras com saldo: {carteiras_com_saldo}\n"
    )
    with _file_lock:
        atomic_write(arquivo, texto)


def salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info):
    """Salva os dados da carteira encontrada com saldo."""
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
# Geração / verificação (Retidas do script anterior)
# ------------------------
def criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2):
    """Cria o mnemonic no padrão 10x base + 2 variáveis."""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    return " ".join(palavras)


def validar_mnemonic(mnemonic):
    """Valida o checksum BIP39 (Roda no loop principal - CPU)."""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except Exception:
        return False


def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Converte mnemonic para seed."""
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)


def derivar_bip44_btc(seed: bytes):
    """Deriva o caminho BIP44 (m/44'/0'/0'/0/0)"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)


def mostrar_info(addr_index):
    """Extrai informações da chave (WIF, Endereço, etc.)."""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    return {
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "wif": priv_key_obj.ToWif(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
        "address": addr_index.PublicKey().ToAddress()
    }


# ------------------------
# I/O Assíncrono (AIOHTTP) - Estabilidade Aprimorada
# ------------------------

async def verificar_saldo_explorer(session: aiohttp.ClientSession, endereco, timeout=15):
    """
    Função assíncrona para consultar saldo com alta resiliência (backoff e retentativas).
    Aumentei o timeout para dar mais folga aos servidores.
    """
    tem_saldo = False
    
    for attempt in range(MAX_API_RETRIES):
        # Tenta uma API diferente a cada tentativa para contornar bloqueios específicos
        api_url = random.choice(EXPLORER_APIS) 
        url = api_url + endereco
        api_name = api_url.split('/')[2]
        
        try:
            # 1. Requisição com Timeout
            async with session.get(url, timeout=timeout) as response:
                
                # 2. Tratamento de Sucesso
                if response.status == 200:
                    data = await response.json()
                    
                    # Lógica de extração de saldo adaptada para diferentes APIs:
                    if "mempool.space" in api_url or "blockstream.info" in api_url:
                        saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    elif "api.blockcypher.com" in api_url:
                        saldo = data.get('final_balance', 0)

                    return saldo > 0 # Sucesso! Sai da função.

                # 3. Tratamento de Erros de Concorrência/Servidor
                elif response.status in [429, 500, 503, 504]:
                    # 429: Too Many Requests; 5xx: Server Errors.
                    if attempt < MAX_API_RETRIES - 1:
                        # Cálculo do backoff exponencial (2^tentativa) com jitter (aleatoriedade)
                        base_sleep = 2 ** attempt
                        jitter = random.uniform(0.1, 0.5)
                        sleep_time = base_sleep + jitter
                        
                        print(f"🟡 AVISO (Instabilidade - {response.status} em {api_name}): Backoff ativado. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_API_RETRIES}).")
                        await asyncio.sleep(sleep_time)
                        continue # Vai para o próximo loop (próxima tentativa)
                    else:
                        print(f"🟠 AVISO (Estabilidade Máxima): {api_name} falhou após {MAX_API_RETRIES} tentativas. Desistindo desta chave.")
                        return False # Desiste

                else:
                    # Outros erros HTTP (400, 404, etc.)
                    # 404 é normal (endereço sem transações).
                    if response.status != 404:
                         print(f"🟠 AVISO: {api_name} retornou status HTTP inesperado {response.status}. Pulando.")
                    return False

        # 4. Tratamento de Erros de Rede/Timeout
        except asyncio.TimeoutError:
            print(f"🔴 Erro: Timeout assíncrono ao verificar {endereco} em {api_name}.")
        except aiohttp.ClientError as e:
            # Captura erros de conexão, DNS, SSL, etc.
            print(f"🔴 Erro de Conexão Crítico ({type(e).__name__} em {api_name}): Backoff ativado.")
        except Exception as e:
            # Erros de conexão ou outros
            print(f"🔴 Erro inesperado ao verificar {endereco} em {api_name}: {e}")
            
        # Lógica de Retentativa após falha de conexão/timeout
        if attempt < MAX_API_RETRIES - 1:
            base_sleep = 2 ** attempt
            jitter = random.uniform(0.1, 0.5)
            sleep_time = base_sleep + jitter
            print(f"🟡 AVISO (Instabilidade de Rede): Aguardando {sleep_time:.2f}s antes de nova tentativa (Tentativa {attempt + 1}/{MAX_API_RETRIES}).")
            await asyncio.sleep(sleep_time)
            continue
        else:
            print(f"🟠 AVISO (Estabilidade Máxima): Falha persistente na rede após {MAX_API_RETRIES} tentativas. Desistindo desta chave.")
            return False

    return tem_saldo # Deve ser False se todas as tentativas falharem


async def process_validacao(semaphore, session, mnemonic, palavra_base, palavra_completa1, palavra_completa2, stats):
    """
    Roda como uma 'task' assíncrona: Deriva chaves, consulta saldo e atualiza estatísticas.
    O Semaphore garante que no máximo 5 (CONCURRENCY_LIMIT) dessas funções rodem I/O ao mesmo tempo.
    """
    async with semaphore: # Aquisição do 'slot' de concorrência
        
        # 1. Derivação de Chaves (rápido - CPU)
        try:
            seed = mnemonic_para_seed(mnemonic)
            addr_index = derivar_bip44_btc(seed)
            info = mostrar_info(addr_index)
        except Exception as e:
            # Em caso de falha na derivação, registra e sai.
            print(f"Erro na derivação de chaves para {mnemonic[:12]}...: {e}")
            return

        # 2. Consulta de Saldo (I/O assíncrono, máximo de estabilidade)
        tem_saldo = await verificar_saldo_explorer(session, info["address"])
        
        # 3. Atualização de Estatísticas (seguro com lock)
        with _stats_lock:
            stats['validas'] += 1
            if tem_saldo:
                stats['saldos'] += 1
        
        # 4. Salvamento
        if tem_saldo:
            salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info)
            
    # O slot do Semaphore é liberado automaticamente ao sair do 'async with'


# ------------------------
# Função principal
# ------------------------
async def async_main():
    """Lógica principal assíncrona do script."""
    print("Iniciando realfindbitcoin.py - MODO ESTÁVEL...")
    print("Carregando palavras BIP39...")
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return

    # Carrega o estado atual (checkpoint)
    ultima_base, ultima_completa1, ultima_completa2, ultimo_mnemonic = carregar_ultima_combinacao(ULTIMO_FILE)
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint(CHECKPOINT_FILE)

    print(f"\nEstatísticas carregadas:\n  Total testadas: {contador_total}\n  Válidas: {contador_validas}\n  Com saldo: {carteiras_com_saldo}\n")

    if ultima_base and ultima_completa1 and ultima_completa2:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        print("Nenhum checkpoint encontrado, começando do início...\n")
        base_idx, completa_idx = 0, 0

    print(f"Continuando de '{palavras[base_idx]}' (base), iniciando variação #{completa_idx+1}.")
    print("\nIniciando geração de combinações 10+2 BIP39 (Estabilidade Aprimorada)...\n")
    print(f"Limite de concorrência (CONCURRENCY_LIMIT): {CONCURRENCY_LIMIT}")

    ultimo_salvamento_tempo = time.time()
    stats = {'validas': contador_validas, 'saldos': carteiras_com_saldo} 
    
    # Cria o Semaphore para limitar tarefas de I/O
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = []
    
    i = base_idx 
    palavra_base = palavras[i]

    # Cria uma sessão AIOHTTP para todas as requisições
    async with aiohttp.ClientSession() as session:
        
        try:
            for i in range(base_idx, len(palavras)):
                palavra_base = palavras[i]
                start_j = completa_idx if i == base_idx else 0
                
                for j in range(start_j, len(palavras) - 1): 
                    palavra_completa1 = palavras[j]
                    palavra_completa2 = palavras[j + 1]
                    contador_total += 1

                    mnemonic = criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2)
                    
                    # Salvamento no ULTIMO_FILE deve ser mais frequente
                    salvar_ultima_combinacao(ULTIMO_FILE, palavra_base, palavra_completa1, palavra_completa2)

                    # Salvamento de Checkpoint (tempo ou frequência)
                    now = time.time()
                    if now - ultimo_salvamento_tempo > SAVE_INTERVAL_SEC or contador_total % FREQUENCY_SAVE == 0:
                        # O lock está embutido na função salvar_checkpoint
                        salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                        ultimo_salvamento_tempo = now

                    if contador_total % FREQUENCY_PRINT == 0:
                        print(f"Testadas {contador_total} combinações | Última: {mnemonic}")
                        with _stats_lock: 
                             print(f"  Válidas (até agora): {stats['validas']} | Com saldo: {stats['saldos']}")

                    # Validação rápida de CPU
                    if validar_mnemonic(mnemonic):
                        # Cria uma nova tarefa assíncrona (Task) para processar e checar saldo
                        task = asyncio.create_task(process_validacao(semaphore, session, mnemonic, palavra_base, palavra_completa1, palavra_completa2, stats))
                        tasks.append(task)
                        
                        # CONTROLE DE ESTABILIDADE: Se muitas tarefas estiverem pendentes,
                        # esperamos um pouco para não esgotar recursos de memória ou loop de eventos.
                        # Isso previne o "hiper-acúmulo" de tasks.
                        if len(tasks) > CONCURRENCY_LIMIT * 10: 
                            # Espera mínima, não-bloqueante
                            await asyncio.sleep(0.005) 
                
                completa_idx = 0 
                
                # Salvamento de checkpoint ao concluir uma palavra base
                salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                print(f"\nConcluído para '{palavra_base}': Válidas até agora: {stats['validas']}, Com saldo: {stats['saldos']}\n")

        except KeyboardInterrupt:
            print("\n🟡 Execução interrompida manualmente. Salvando progresso...")

        finally:
            # 5. Finalização: espera por todas as tasks criadas
            print("🟢 Aguardando finalização das tarefas de consulta de saldo pendentes (Finalizando I/O estável)...")
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # Finalização do checkpoint e estatísticas
    final_base_idx = i if 'i' in locals() else 0
    final_palavra_base = palavra_base if 'palavra_base' in locals() else ""
    
    salvar_checkpoint(CHECKPOINT_FILE, final_base_idx, final_palavra_base, contador_total, stats['validas'], stats['saldos'])

    with open("estatisticas_finais.txt", "w", encoding='utf-8') as f:
        f.write("ESTATÍSTICAS FINAIS\n" + "=" * 50 + "\n")
        f.write(f"Total testadas: {contador_total}\n")
        f.write(f"Válidas: {stats['validas']}\n")
        f.write(f"Com saldo: {stats['saldos']}\n")

    print("\n✅ Execução finalizada. Estatísticas gravadas em estatisticas_finais.txt")
    print(f"Total testadas: {contador_total} | Válidas: {stats['validas']} | Com saldo: {stats['saldos']}")


def main():
    """Função de entrada que inicia o loop assíncrono."""
    try:
        asyncio.run(async_main())
    except Exception as e:
        print(f"ERRO CRÍTICO NO LOOP PRINCIPAL: {e}")

if __name__ == "__main__":
    main()
