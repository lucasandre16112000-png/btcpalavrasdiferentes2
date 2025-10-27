#!/usr/bin/env python3
"""
Bitcoin Wallet Finder - Versão Otimizada e Assíncrona
Suporta modos: 11+1 e 10+2
Autor: Otimizado por Manus AI
"""

import asyncio
import os
import time
import json
import httpx
from bip_utils import (
    Bip39SeedGenerator, Bip39MnemonicValidator,
    Bip44, Bip44Coins, Bip44Changes
)
from typing import List, Tuple, Optional

# --- CONFIGURAÇÕES GLOBAIS ---
# Limite de requisições simultâneas para a API da Mempool.space
# Ajuste este valor para equilibrar velocidade e evitar 429
CONCURRENCY_LIMIT = 5 
# Tempo de espera inicial em caso de 429 (será dobrado a cada tentativa)
INITIAL_BACKOFF_DELAY = 1.0 
MAX_RETRIES = 5

# Semáforo para controlar a concorrência
semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

# --- FUNÇÕES DE UTILIDADE E CHECKPOINT ---

def carregar_palavras_bip39(arquivo="bip39-words.txt") -> List[str]:
    """Carrega a lista de palavras BIP39 do arquivo"""
    if not os.path.exists(arquivo):
        # Tenta criar um arquivo bip39-words.txt se não existir
        try:
            from bip_utils.bip.bip39 import Bip39WordsNum
            from bip_utils import Bip39Languages
            palavras = Bip39WordsNum.FromWordsNumber(2048).GetList(Bip39Languages.ENGLISH)
            with open(arquivo, 'w') as f:
                f.write('\n'.join(palavras))
            print(f"✓ Arquivo '{arquivo}' criado com a lista BIP39 padrão em inglês.")
            return list(palavras)
        except Exception as e:
            raise FileNotFoundError(f"Arquivo {arquivo} não encontrado e não foi possível gerar a lista padrão: {e}")
    
    with open(arquivo, 'r') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"⚠ Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    return palavras

def carregar_checkpoint(arquivo="checkpoint.json") -> Tuple[int, int, int, Optional[str], Optional[str], Optional[str]]:
    """Carrega estatísticas e a última combinação testada do arquivo JSON"""
    if not os.path.exists(arquivo):
        return 0, 0, 0, None, None, None

    try:
        with open(arquivo, 'r') as f:
            data = json.load(f)
            contador_total = data.get('contador_total', 0)
            contador_validas = data.get('contador_validas', 0)
            carteiras_com_saldo = data.get('carteiras_com_saldo', 0)
            palavra_base = data.get('palavra_base')
            palavra_var1 = data.get('palavra_var1')
            palavra_var2 = data.get('palavra_var2')
            return contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavra_var1, palavra_var2
    except Exception as e:
        print(f"❌ Erro ao ler checkpoint: {e}. Reiniciando do zero.")
        return 0, 0, 0, None, None, None

def salvar_checkpoint(arquivo: str, contador_total: int, contador_validas: int, carteiras_com_saldo: int,
                      palavra_base: str, palavra_var1: str, palavra_var2: Optional[str] = None):
    """Salva checkpoint com estatísticas atuais e a última combinação testada"""
    data = {
        'contador_total': contador_total,
        'contador_validas': contador_validas,
        'carteiras_com_saldo': carteiras_com_saldo,
        'palavra_base': palavra_base,
        'palavra_var1': palavra_var1,
        'palavra_var2': palavra_var2
    }
    with open(arquivo, 'w') as f:
        json.dump(data, f, indent=4)

# --- FUNÇÕES DE CRIAÇÃO E VERIFICAÇÃO DE MNEMONIC ---

def criar_mnemonic(palavra_base: str, palavra_var1: str, palavra_var2: Optional[str], modo: str) -> str:
    """Cria mnemonic baseado no modo (11+1 ou 10+2)"""
    if modo == "11+1":
        # 11 palavras base + 1 variável (palavra_var1)
        palavras = [palavra_base] * 11 + [palavra_var1]
    elif modo == "10+2":
        # 10 palavras base + 2 variáveis (palavra_var1 e palavra_var2)
        if palavra_var2 is None:
            raise ValueError("Modo 10+2 requer palavra_var2")
        palavras = [palavra_base] * 10 + [palavra_var1, palavra_var2]
    else:
        raise ValueError("Modo inválido. Use '11+1' ou '10+2'")
    
    return " ".join(palavras)

def validar_mnemonic(mnemonic: str) -> bool:
    """Valida se o mnemonic é válido segundo BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False

# --- FUNÇÕES DE DERIVAÇÃO DE CARTEIRA BIP44 ---

def mnemonic_para_seed(mnemonic: str) -> bytes:
    """Converte mnemonic para seed bytes"""
    return Bip39SeedGenerator(mnemonic).Generate()

def derivar_bip44_btc(seed: bytes):
    """Deriva endereço Bitcoin usando BIP44 (m/44'/0'/0'/0/0)"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index, mnemonic: str, palavra_base: str, palavra_var1: str, palavra_var2: Optional[str]):
    """Extrai informações da carteira e formata para salvamento"""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    
    return {
        "palavra_base": palavra_base,
        "palavra_var1": palavra_var1,
        "palavra_var2": palavra_var2,
        "mnemonic": mnemonic,
        "address": addr_index.PublicKey().ToAddress(),
        "wif": priv_key_obj.ToWif(),
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
    }

# --- FUNÇÕES DE REDE ASSÍNCRONA COM RATE LIMITING E BACKOFF ---

async def verificar_saldo_mempool_async(client: httpx.AsyncClient, endereco: str) -> bool:
    """Verifica saldo do endereço usando API da Mempool.space com retry e backoff"""
    url = f"https://mempool.space/api/address/{endereco}"
    delay = INITIAL_BACKOFF_DELAY

    for attempt in range(MAX_RETRIES):
        async with semaphore:
            try:
                response = await client.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    # A API da Mempool.space retorna 'chain_stats' com 'funded_txo_sum'
                    funded_sum = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    return funded_sum > 0
                
                elif response.status_code == 429:
                    print(f"🟡 AVISO (429 - Mempool.space): Backoff ativado. Tentando novamente em {delay:.2f}s (Tentativa {attempt + 1}/{MAX_RETRIES}).")
                    await asyncio.sleep(delay)
                    delay *= 2 # Exponential backoff
                
                else:
                    # Tratar outros erros de API
                    print(f"⚠ Erro de API ({response.status_code}) ao verificar {endereco}")
                    return False

            except httpx.ConnectError as e:
                print(f"⚠ Erro de conexão ao verificar {endereco}. Tentando novamente em {delay:.2f}s.")
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as e:
                print(f"❌ Erro inesperado ao verificar saldo: {e}")
                return False
    
    print(f"❌ ERRO CRÍTICO: Falha ao verificar saldo para {endereco} após {MAX_RETRIES} tentativas.")
    return False

async def processar_combinacao(client: httpx.AsyncClient, mnemonic: str, palavra_base: str, palavra_var1: str, palavra_var2: Optional[str], modo: str):
    """Processa uma combinação válida: deriva carteira e verifica saldo"""
    
    # Gerar carteira
    seed = mnemonic_para_seed(mnemonic)
    addr_index = derivar_bip44_btc(seed)
    info = mostrar_info(addr_index, mnemonic, palavra_base, palavra_var1, palavra_var2)
    
    # Verificar saldo de forma assíncrona
    tem_saldo = await verificar_saldo_mempool_async(client, info["address"])
    
    return info, tem_saldo

def salvar_carteira_com_saldo(info: dict):
    """Salva carteira com saldo no arquivo saldo.txt"""
    with open("saldo.txt", "a") as f:
        f.write("=" * 80 + "\n")
        f.write("💎 CARTEIRA COM SALDO ENCONTRADA - DETALHES COMPLETOS 💎\n")
        f.write(f"Palavra Base: {info['palavra_base']}\n")
        f.write(f"Palavra Variável 1: {info['palavra_var1']}\n")
        if info['palavra_var2']:
            f.write(f"Palavra Variável 2: {info['palavra_var2']}\n")
        f.write(f"Mnemonic: {info['mnemonic']}\n")
        f.write(f"Endereço: {info['address']}\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("=" * 80 + "\n\n")
    print("\n🎉 CARTEIRA COM SALDO SALVA! 🎉")

# --- FUNÇÃO PRINCIPAL ---

async def main_async():
    """Função principal assíncrona para processamento rápido"""
    
    # --- Configuração Inicial ---
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER - Versão Otimizada")
    print("=" * 80)
    
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"✓ Carregadas {len(palavras)} palavras BIP39.")
    except FileNotFoundError as e:
        print(e)
        return

    # Escolha do modo de operação (11+1 ou 10+2)
    print("\n📋 Modos disponíveis:")
    print("  • 11+1: 11 palavras repetidas + 1 palavra variável")
    print("  • 10+2: 10 palavras repetidas + 2 palavras variáveis")
    MODO = input("\n👉 Escolha o modo de operação ('11+1' ou '10+2'): ").strip()
    if MODO not in ["11+1", "10+2"]:
        print("❌ Modo inválido. Encerrando.")
        return

    # Carregar checkpoint
    contador_total, contador_validas, carteiras_com_saldo, cp_base, cp_var1, cp_var2 = carregar_checkpoint("checkpoint.json")
    
    print(f"\n{'=' * 80}")
    print("📊 ESTATÍSTICAS CARREGADAS")
    print(f"{'=' * 80}")
    print(f"  Modo de Operação: {MODO}")
    print(f"  Total de combinações testadas: {contador_total:,}")
    print(f"  Combinações válidas (BIP39): {contador_validas:,}")
    print(f"  Carteiras com saldo: {carteiras_com_saldo}")
    
    # Determinar ponto de partida
    start_i = palavras.index(cp_base) if cp_base in palavras else 0
    start_j = palavras.index(cp_var1) if cp_var1 in palavras else 0
    start_k = palavras.index(cp_var2) if cp_var2 in palavras else 0
    
    if cp_base:
        print(f"\n🔄 Continuando da última posição:")
        print(f"  Base: '{cp_base}' | Var1: '{cp_var1}' | Var2: '{cp_var2}' (se aplicável)")
    else:
        print("\n🆕 Nenhum checkpoint encontrado, começando do início...")
    
    print(f"\n⚙️  Configurações:")
    print(f"  Limite de concorrência: {CONCURRENCY_LIMIT} requisições simultâneas")
    print(f"  Backoff inicial: {INITIAL_BACKOFF_DELAY}s")
    print(f"  Máximo de tentativas: {MAX_RETRIES}")
    print(f"\n💡 Pressione Ctrl+C para parar com segurança.\n")
    print("=" * 80)
    
    # Variável para rastrear tempo e taxa
    tempo_inicio = time.time()
    ultimo_checkpoint_tempo = tempo_inicio

    # --- Loop Principal e Processamento Assíncrono ---
    
    # Lista para armazenar as tarefas assíncronas (verificação de saldo)
    tasks = []
    # Usar httpx.AsyncClient para gerenciar conexões eficientemente
    async with httpx.AsyncClient() as client:
        try:
            # Loop principal para gerar combinações
            for i in range(start_i, len(palavras)):
                palavra_base = palavras[i]
                
                # Otimização para 11+1: apenas uma variável
                if MODO == "11+1":
                    start_j_inner = start_j if i == start_i else 0
                    for j in range(start_j_inner, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        contador_total += 1
                        mnemonic = criar_mnemonic(palavra_base, palavra_var1, None, MODO)
                        
                        if validar_mnemonic(mnemonic):
                            contador_validas += 1
                            # Adiciona a tarefa de processamento à lista
                            task = asyncio.create_task(
                                processar_combinacao(client, mnemonic, palavra_base, palavra_var1, None, MODO)
                            )
                            tasks.append(task)
                        
                        # Exibir progresso e salvar checkpoint
                        if contador_total % 100 == 0:
                            tempo_decorrido = time.time() - tempo_inicio
                            taxa = contador_total / tempo_decorrido if tempo_decorrido > 0 else 0
                            print(f"📈 Testadas: {contador_total:,} | Válidas: {contador_validas:,} | Com saldo: {carteiras_com_saldo} | Taxa: {taxa:.1f} comb/s")
                            salvar_checkpoint("checkpoint.json", contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavra_var1, None)
                        
                        # Processar resultados das tarefas que já terminaram
                        tasks = await processar_tarefas_concluidas(tasks, carteiras_com_saldo)

                    # Salvar checkpoint após cada palavra base
                    salvar_checkpoint("checkpoint.json", contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavras[-1], None)
                    print(f"\n✓ Concluído para '{palavra_base}' (Modo 11+1).")
                    start_j = 0 # Resetar para a próxima palavra base
                
                # Lógica para 10+2: duas variáveis
                elif MODO == "10+2":
                    start_j_outer = start_j if i == start_i else 0
                    for j in range(start_j_outer, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        start_k_inner = start_k if i == start_i and j == start_j_outer else 0
                        for k in range(start_k_inner, len(palavras)):
                            palavra_var2 = palavras[k]
                            
                            contador_total += 1
                            mnemonic = criar_mnemonic(palavra_base, palavra_var1, palavra_var2, MODO)
                            
                            if validar_mnemonic(mnemonic):
                                contador_validas += 1
                                # Adiciona a tarefa de processamento à lista
                                task = asyncio.create_task(
                                    processar_combinacao(client, mnemonic, palavra_base, palavra_var1, palavra_var2, MODO)
                                )
                                tasks.append(task)
                            
                            # Exibir progresso e salvar checkpoint
                            if contador_total % 100 == 0:
                                tempo_decorrido = time.time() - tempo_inicio
                                taxa = contador_total / tempo_decorrido if tempo_decorrido > 0 else 0
                                print(f"📈 Testadas: {contador_total:,} | Válidas: {contador_validas:,} | Com saldo: {carteiras_com_saldo} | Taxa: {taxa:.1f} comb/s")
                                salvar_checkpoint("checkpoint.json", contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavra_var1, palavra_var2)
                            
                            # Processar resultados das tarefas que já terminaram
                            tasks = await processar_tarefas_concluidas(tasks, carteiras_com_saldo)
                        
                        start_k = 0 # Resetar para a próxima palavra var1
                    
                    # Salvar checkpoint após cada palavra base
                    salvar_checkpoint("checkpoint.json", contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavras[-1], palavras[-1])
                    print(f"\n✓ Concluído para '{palavra_base}' (Modo 10+2).")
                    start_j = 0 # Resetar para a próxima palavra base

        except KeyboardInterrupt:
            print("\n\n⚠️  Programa interrompido pelo usuário.")
        
        finally:
            # Esperar que todas as tarefas pendentes terminem
            print(f"\n⏳ Finalizando {len(tasks)} tarefas pendentes...")
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Garantir que os resultados finais sejam processados
            tasks = await processar_tarefas_concluidas(tasks, carteiras_com_saldo, final=True)
            
            # Salvar estatísticas finais
            salvar_checkpoint("checkpoint.json", contador_total, contador_validas, carteiras_com_saldo, palavra_base, palavra_var1, palavra_var2 if MODO == "10+2" else None)
            
            tempo_total = time.time() - tempo_inicio
            print(f"\n{'=' * 80}")
            print("📊 ESTATÍSTICAS FINAIS")
            print(f"{'=' * 80}")
            print(f"  Total de combinações testadas: {contador_total:,}")
            print(f"  Combinações válidas (BIP39): {contador_validas:,}")
            print(f"  Carteiras com saldo encontradas: {carteiras_com_saldo}")
            print(f"  Tempo total de execução: {tempo_total/60:.2f} minutos")
            print(f"  Taxa média: {contador_total/tempo_total:.1f} combinações/segundo")
            print("=" * 80)

async def processar_tarefas_concluidas(tasks: List[asyncio.Task], carteiras_com_saldo: int, final: bool = False) -> List[asyncio.Task]:
    """Processa tarefas que já terminaram e retorna a lista de tarefas pendentes."""
    
    # Se não for o final, processa apenas as tarefas que já terminaram
    if not final:
        done_tasks = [task for task in tasks if task.done()]
        pending_tasks = [task for task in tasks if not task.done()]
    else:
        # No final, processa todas as tarefas
        done_tasks = tasks
        pending_tasks = []
    
    # Processar resultados das tarefas concluídas
    for task in done_tasks:
        try:
            info, tem_saldo = await task
            if tem_saldo:
                carteiras_com_saldo += 1
                salvar_carteira_com_saldo(info)
                print(f"\n💎 CARTEIRA COM SALDO ENCONTRADA!")
                print(f"   Endereço: {info['address']}")
                print(f"   Mnemonic: {info['mnemonic']}\n")
        except Exception as e:
            # Ignorar erros de tarefas individuais
            pass
    
    return pending_tasks

# --- PONTO DE ENTRADA ---

def main():
    """Ponto de entrada do programa"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n\n👋 Programa encerrado pelo usuário.")
    except Exception as e:
        print(f"\n❌ Erro fatal: {e}")

if __name__ == "__main__":
    main()
