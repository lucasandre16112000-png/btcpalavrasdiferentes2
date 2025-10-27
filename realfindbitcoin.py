#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin_10+2_SIMPLIFICADO.py (versão com LÓGICA 10+2)
- LÓGICA: 10 palavras base (iguais) + 2 variáveis (em loop total 2048x2048).
- FOCO PRINCIPAL: Estabilidade de rede, alta velocidade de varredura e salvamento TXT.
- AÇÃO: Apenas salva as chaves (WIF/HEX) no arquivo 'saldo.txt' ao encontrar saldo.
"""
import os
import time
import json
import asyncio
import random
import aiohttp
import threading
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes

# ------------------------
# CONFIGURAÇÃO RÁPIDA
# ------------------------
CHECKPOINT_FILE = "checkpoint.txt"
ULTIMO_FILE = "ultimo.txt"
SALDO_FILE = "saldo.txt"
FREQUENCY_PRINT = 100 # print a cada N combinações
FREQUENCY_SAVE = 100 # salvar checkpoint a cada N combinações
SAVE_INTERVAL_SEC = 30 # ou salvar a cada X segundos (tempo)

# >>> VARIÁVEL CRÍTICA DE ESTABILIDADE <<<
CONCURRENCY_LIMIT = 2 # Limite de tarefas de I/O ativas simultâneas
MAX_API_RETRIES = 5 # Número máximo de vezes para tentar a consulta de saldo

# Lista de APIs de exploradores de blockchain (usada para diversificar as requisições)
EXPLORER_APIS = [
    "https://mempool.space/api/address/",
    "https://blockstream.info/api/address/",
    "https://api.blockcypher.com/v1/btc/main/addrs/"
]

# locks para controle de concorrência (necessários para I/O de disco e stats)
_stats_lock = threading.Lock()
_file_lock = threading.Lock()

# ------------------------
# Helpers de arquivo e texto
# ------------------------
def safe_write(path: str, content: str, mode='w', encoding='utf-8'):
    """Escreve/adiciona conteúdo no arquivo de forma síncrona, dentro do lock."""
    with _file_lock:
        with open(path, mode, encoding=encoding) as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                # Falha silenciosa se o SO não suportar fsync
                pass

def salvar_ultima_combinacao(arquivo=ULTIMO_FILE, palavra_base="", var1="", var2=""):
    """Salva a última combinação testada (Palavra Base + Variável 11 + Variável 12)."""
    mnemonic_snippet = f"{palavra_base} {var1} {var2}"
    safe_write(arquivo, mnemonic_snippet, 'w')

def salvar_checkpoint(arquivo=CHECKPOINT_FILE, base_idx=0, palavra_base="", contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    """Salva o progresso e estatísticas."""
    texto = (
        f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n"
        f"Total de combinações testadas: {contador_total}\n"
        f"Combinações válidas: {contador_validas}\n"
        f"Carteiras com saldo: {carteiras_com_saldo}\n"
    )
    safe_write(arquivo, texto, 'w')

def _create_carteira_text(palavra_base, var1, var2, mnemonic, info):
    """Cria o texto formatado para console/arquivo."""
    texto = (
        f"Palavra Base: {palavra_base} (repetida 10x)\n"
        f"Palavra 11: {var1}\n"
        f"Palavra 12: {var2}\n"
        f"Mnemonic: {mnemonic}\n"
        f"Endereço: {info['address']}\n"
        f"Chave Privada (WIF): {info['wif']}\n"
        f"Chave Privada (HEX): {info['priv_hex']}\n"
        f"Chave Pública: {info['pub_compressed_hex']}\n"
        + "-" * 80 + "\n\n"
    )
    return texto

def salvar_carteira_com_saldo_file(texto):
    """Salva o texto no arquivo de saldo. SÓ CHAMADO SE TIVER SALDO!"""
    safe_write(SALDO_FILE, texto, 'a')

# ------------------------
# Funções de I/O e estado
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
    """Carrega o último mnemonic snippet testado (Base + Variável 11 + Variável 12)."""
    if not os.path.exists(arquivo):
        return None, None, None, None
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            palavras = f.read().strip().split()
            if len(palavras) == 3:
                palavra_base = palavras[0]
                var1 = palavras[1]
                var2 = palavras[2]
                # Retorna os 3 componentes e a string completa (snippet)
                return palavra_base, var1, var2, " ".join(palavras)
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
                    contador_total = int(line.split(":")[1].strip())
                elif "Combinações válidas:" in line:
                    contador_validas = int(line.split(":")[1].strip())
                elif "Carteiras com saldo:" in line:
                    carteiras_com_saldo = int(line.split(":")[1].strip())
    except Exception as e:
        print(f"Erro ao ler checkpoint: {e}")
        return contador_total, contador_validas, carteiras_com_saldo
    
    return contador_total, contador_validas, carteiras_com_saldo

def encontrar_proxima_combinacao(palavras, ultima_base, ultima_var1, ultima_var2):
    """
    Encontra o próximo índice de onde continuar a varredura (Lógica 10+2 de exaustão).
    Prioriza o loop mais interno (var2), depois var1, depois base.
    """
    try:
        base_idx = palavras.index(ultima_base)
        var1_idx = palavras.index(ultima_var1)
        var2_idx = palavras.index(ultima_var2)

        # 1. Tenta avançar a Palavra 12 (Loop mais interno)
        if var2_idx + 1 < len(palavras):
            return base_idx, var1_idx, var2_idx + 1

        # 2. Se a Palavra 12 terminou, reseta Palavra 12 e avança Palavra 11
        if var1_idx + 1 < len(palavras):
            return base_idx, var1_idx + 1, 0

        # 3. Se Palavra 11 terminou, reseta Palavra 11 e 12, e avança Palavra Base
        if base_idx + 1 < len(palavras):
            return base_idx + 1, 0, 0
        else:
            return None, None, None # Fim da varredura
            
    except ValueError:
        # Se alguma das palavras do checkpoint não for encontrada na lista (erro de arquivo),
        # ou se o arquivo estava vazio/corrompido.
        print("🟡 Aviso: Palavra do checkpoint não encontrada. Iniciando varredura do começo (0, 0, 0).")
        return 0, 0, 0

# ------------------------
# Geração / verificação
# ------------------------
def criar_mnemonic_repetido(palavra_base, palavra_variavel_1, palavra_variavel_2):
    """Cria o mnemonic no padrão 10x base + 1x var1 + 1x var2."""
    palavras = [palavra_base] * 10 + [palavra_variavel_1] + [palavra_variavel_2]
    # O mnemonic de 12 palavras gerado aqui só será válido se o checksum for correto.
    return " ".join(palavras)

def validar_mnemonic(mnemonic):
    """Valida o checksum BIP39 (Roda no loop principal - CPU)."""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except Exception:
        # Catch any unexpected error during validation (e.g., bad word list)
        return False

def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Converte o mnemonic validado em seed."""
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)

def derivar_bip44_btc(seed: bytes):
    """Deriva a chave HD (m/44'/0'/0'/0/0)."""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index):
    """Extrai informações da carteira."""
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
    Função assíncrona para consultar saldo com retentativas e backoff.
    Retorna True se o saldo > 0, False caso contrário.
    """
    for attempt in range(MAX_API_RETRIES):
        api_url = random.choice(EXPLORER_APIS)
        url = api_url + endereco
        api_name = api_url.split('/')[2]
        
        try:
            # Tenta a requisição com timeout
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
                    saldo = 0
                    
                    # Lógica de extração de saldo específica para cada API
                    if "mempool.space" in api_url or "blockstream.info" in api_url:
                        saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    elif "api.blockcypher.com" in api_url:
                        saldo = data.get('final_balance', 0)
                        
                    return saldo > 0 

                elif response.status in [429, 500, 503, 504]:
                    # Erro de Servidor ou Rate Limit - Entra no Backoff
                    if attempt < MAX_API_RETRIES - 1:
                        base_sleep = 2 ** attempt
                        jitter = random.uniform(0.1, 0.5)
                        sleep_time = base_sleep + jitter
                        print(f"🟡 AVISO (Instabilidade - {response.status} em {api_name}): Backoff ativado. Tentando novamente em {sleep_time:.2f}s (Tentativa {attempt + 1}/{MAX_API_RETRIES}).")
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        # Ultrapassou o limite de retentativas
                        print(f"🟠 AVISO (Estabilidade Máxima): {api_name} falhou após {MAX_API_RETRIES} tentativas. Desistindo desta chave.")
                        return False
                else:
                    # Outros status (ex: 404 para endereço não usado)
                    if response.status != 404:
                        print(f"🟠 AVISO: {api_name} retornou status HTTP inesperado {response.status}. Pulando.")
                    return False

        except asyncio.TimeoutError:
            print(f"🔴 Erro: Timeout assíncrono ao verificar {endereco} em {api_name}.")
        except aiohttp.ClientError as e:
            print(f"🔴 Erro de Conexão Crítico ({type(e).__name__} em {api_name}): Backoff ativado.")
        except Exception as e:
            print(f"🔴 Erro inesperado ao verificar {endereco} em {api_name}: {e}")

        # Se houve exceção, aplicar backoff antes da próxima tentativa
        if attempt < MAX_API_RETRIES - 1:
            base_sleep = 2 ** attempt
            jitter = random.uniform(0.1, 0.5)
            sleep_time = base_sleep + jitter
            await asyncio.sleep(sleep_time)
            continue
        else:
            return False
            
    return False

async def process_validacao(semaphore, session, mnemonic, palavra_base, var1, var2, stats):
    """ 
    Roda como uma 'task' assíncrona: Deriva chaves, consulta saldo e, se positivo, 
    SALVA os dados no arquivo TXT de forma thread-safe.
    """
    async with semaphore:
        # 1. Derivação de Chaves (CPU-bound)
        try:
            seed = mnemonic_para_seed(mnemonic)
            addr_index = derivar_bip44_btc(seed)
            info = mostrar_info(addr_index)
        except Exception as e:
            print(f"Erro na derivação de chaves para {mnemonic.split()[0]}......: {e}")
            return

        # 2. Consulta de Saldo (I/O-bound)
        tem_saldo = await verificar_saldo_explorer(session, info["address"])

        # 3. Processamento de Saldo (Apenas salvar em TXT)
        if tem_saldo:
            # Atualização de estatísticas thread-safe
            with _stats_lock:
                stats['saldos'] += 1

            texto = _create_carteira_text(palavra_base, var1, var2, mnemonic, info)
            
            # IMPRESSÃO COMPLETA NO CONSOLE
            print("\n" + "=" * 80)
            print("💎 CARTEIRA COM SALDO ENCONTRADA - DETALHES COMPLETOS 💎")
            print(texto.strip()) 
            print("=" * 80 + "\n")
            
            # Salvamento no arquivo (AÇÃO FINAL - utiliza safe_write com _file_lock)
            salvar_carteira_com_saldo_file(texto)

# ------------------------
# Função principal
# ------------------------
async def async_main():
    """Lógica principal assíncrona do script com 3 loops (10+2)."""
    print("Iniciando realfindbitcoin_10+2_SIMPLIFICADO.py - MODO LÓGICA 10+2 e ALTA VELOCIDADE (Apenas Salvamento TXT)...")
    
    # Carregamento de palavras e estatísticas
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
    except FileNotFoundError as e:
        print(e)
        return

    ultima_base, ultima_var1, ultima_var2, _ = carregar_ultima_combinacao(ULTIMO_FILE)
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint(CHECKPOINT_FILE)

    stats = {'validas': contador_validas, 'saldos': carteiras_com_saldo}
    
    if ultima_base:
        print(f"Última combinação testada: {ultima_base} {ultima_var1} {ultima_var2}")
        base_idx, var1_idx, var2_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_var1, ultima_var2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        base_idx, var1_idx, var2_idx = 0, 0, 0
    
    print(f"\nContinuando da Base: '{palavras[base_idx]}' | Variável 11: '{palavras[var1_idx]}' | Variável 12: '{palavras[var2_idx]}'")
    print(f"Limite de concorrência: {CONCURRENCY_LIMIT}")
    print("\nPressione Ctrl+C para parar com segurança.\n")

    ultimo_salvamento_tempo = time.time()
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = []

    async with aiohttp.ClientSession() as session:
        try:
            # LOOP 1: Palavra Base (Posições 1-10)
            for i in range(base_idx, len(palavras)):
                palavra_base = palavras[i]
                start_j = var1_idx if i == base_idx else 0
                
                # LOOP 2: Palavra Variável 1 (Posição 11)
                for j in range(start_j, len(palavras)):
                    var1 = palavras[j]
                    start_k = var2_idx if i == base_idx and j == start_j else 0
                    
                    # LOOP 3: Palavra Variável 2 (Posição 12)
                    for k in range(start_k, len(palavras)):
                        var2 = palavras[k]
                        contador_total += 1
                        
                        salvar_ultima_combinacao(ULTIMO_FILE, palavra_base, var1, var2)

                        # Salvamento de Checkpoint (tempo ou frequência)
                        now = time.time()
                        if now - ultimo_salvamento_tempo > SAVE_INTERVAL_SEC or contador_total % FREQUENCY_SAVE == 0:
                            with _stats_lock:
                                salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                            ultimo_salvamento_tempo = now

                        # Geração da frase completa e Validação BIP39 (CPU-BOUND)
                        mnemonic = criar_mnemonic_repetido(palavra_base, var1, var2)
                        
                        if validar_mnemonic(mnemonic):
                            with _stats_lock:
                                stats['validas'] += 1
                            
                            # CRIAÇÃO DE TAREFA ASYNC PARA I/O (CONSULTA SALDO E SALVAMENTO)
                            task = asyncio.create_task(process_validacao(semaphore, session, mnemonic, palavra_base, var1, var2, stats))
                            tasks.append(task)
                            
                            # Pequena pausa para evitar que a lista de tarefas cresça demais
                            # O semáforo gerencia a concorrência ATIVA
                            if len(tasks) > CONCURRENCY_LIMIT * 10:
                                await asyncio.sleep(0.05)
                        # =================================================================

                        if contador_total % FREQUENCY_PRINT == 0:
                            with _stats_lock:
                                print(f"Testadas {contador_total} combinações | Última: {palavra_base} {var1} {var2}")
                                print(f" Válidas (BIP39): {stats['validas']} | Com saldo: {stats['saldos']}")
                    
                    # Resetar k (var2_idx) para o próximo loop j
                    var2_idx = 0
                
                # Resetar j (var1_idx) para o próximo loop i
                var1_idx = 0
                
                # Salvamento de checkpoint ao concluir uma palavra base
                with _stats_lock:
                    salvar_checkpoint(CHECKPOINT_FILE, i, palavra_base, contador_total, stats['validas'], stats['saldos'])
                print(f"\nConcluído para Base '{palavra_base}': Válidas até agora: {stats['validas']}, Com saldo: {stats['saldos']}\n")

        except KeyboardInterrupt:
            print("\n🟡 Execução interrompida manualmente. Salvando progresso...")
            # Garantir que as variáveis de checkpoint estejam definidas
            final_i = i if 'i' in locals() else base_idx
            final_palavra = palavra_base if 'palavra_base' in locals() else palavras[base_idx]
            
            # Garantir que os contadores sejam atualizados pela última vez
            with _stats_lock:
                salvar_checkpoint(CHECKPOINT_FILE, final_i, final_palavra, contador_total, stats['validas'], stats['saldos'])
        finally:
            print("🟢 Aguardando finalização das tarefas de consulta de saldo pendentes...")
            # Cancelar tarefas pendentes e aguardar o final das que estão em andamento
            if tasks:
                pending_tasks = [t for t in tasks if not t.done()]
                if pending_tasks:
                    # Permite que as tarefas atuais no semáforo terminem, mas não inicia novas.
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

            # Salvamento final e estatísticas
            final_base_idx = i if 'i' in locals() else base_idx
            final_palavra_base = palavra_base if 'palavra_base' in locals() else palavras[base_idx]
            
            with _stats_lock:
                salvar_checkpoint(CHECKPOINT_FILE, final_base_idx, final_palavra_base, contador_total, stats['validas'], stats['saldos'])
            
            # Salva estatísticas finais em um arquivo separado
            with open("estatisticas_finais.txt", "w", encoding='utf-8') as f:
                f.write("ESTATÍSTICAS FINAIS\n" + "=" * 50 + "\n")
                f.write(f"Total testadas: {contador_total}\n")
                f.write(f"Válidas (BIP39): {stats['validas']}\n")
                f.write(f"Com saldo: {stats['saldos']}\n")
            
            print("\n✅ Execução finalizada. Estatísticas gravadas em estatisticas_finais.txt")
            with _stats_lock:
                print(f"Total testadas: {contador_total} | Válidas (BIP39): {stats['validas']} | Com saldo: {stats['saldos']}")

def main():
    try:
        # Ponto de entrada do Asyncio
        asyncio.run(async_main())
    except Exception as e:
        print(f"ERRO CRÍTICO NO LOOP PRINCIPAL: {e}")

if __name__ == "__main__":
    main()
