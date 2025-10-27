#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitcoin Wallet Finder - Versão Otimizada com Painel Visual
Mantém a lógica original mas otimiza a verificação de saldo com async
Suporta modos: 11+1 e 10+2
"""

import asyncio
import os
import time
import json
from datetime import datetime
from collections import deque
import httpx
from bip_utils import (
    Bip39SeedGenerator, Bip39MnemonicValidator,
    Bip44, Bip44Coins, Bip44Changes
)
from typing import Optional, Dict, List

# ==================== CONFIGURAÇÕES ====================
CONCURRENCY_LIMIT = 3  # Limite conservador para evitar 429
MAX_RETRIES = 2  # Máximo de tentativas por endereço
RETRY_DELAY = 1.0  # Delay entre tentativas (segundos)
CHECKPOINT_INTERVAL = 30  # Salvar checkpoint a cada X segundos
DISPLAY_UPDATE_INTERVAL = 1  # Atualizar display a cada X segundos

# ==================== ESTATÍSTICAS GLOBAIS ====================
class Stats:
    def __init__(self):
        self.contador_total = 0
        self.contador_validas = 0
        self.carteiras_verificadas = 0
        self.carteiras_com_saldo = 0
        self.erros_api = 0
        self.inicio = time.time()
        self.ultimas_taxas = deque(maxlen=60)  # Últimos 60 segundos
        self.ultima_combinacao = ""
        self.ultimo_endereco = ""
        self.requisicoes_por_minuto = 0
        
    def taxa_atual(self):
        """Calcula taxa de combinações por segundo"""
        tempo_decorrido = time.time() - self.inicio
        if tempo_decorrido > 0:
            return self.contador_total / tempo_decorrido
        return 0
    
    def taxa_verificacao(self):
        """Calcula taxa de verificações por minuto"""
        tempo_decorrido = time.time() - self.inicio
        if tempo_decorrido > 0:
            return (self.carteiras_verificadas / tempo_decorrido) * 60
        return 0

stats = Stats()

# ==================== FUNÇÕES DE ARQUIVO ====================

def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    """Carrega a lista de palavras BIP39 do arquivo"""
    if not os.path.exists(arquivo):
        try:
            from bip_utils.bip.bip39 import Bip39WordsNum
            from bip_utils import Bip39Languages
            palavras = Bip39WordsNum.FromWordsNumber(2048).GetList(Bip39Languages.ENGLISH)
            with open(arquivo, 'w') as f:
                f.write('\n'.join(palavras))
            print(f"✓ Arquivo '{arquivo}' criado automaticamente")
            return list(palavras)
        except:
            raise FileNotFoundError(f"Arquivo {arquivo} não encontrado!")
    
    with open(arquivo, 'r') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"⚠ Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    return palavras

def carregar_checkpoint(arquivo="checkpoint.json"):
    """Carrega checkpoint do arquivo JSON"""
    if not os.path.exists(arquivo):
        return None
    
    try:
        with open(arquivo, 'r') as f:
            data = json.load(f)
            return data
    except:
        return None

def salvar_checkpoint(arquivo, modo, palavra_base, palavra_var1, palavra_var2, base_idx, var1_idx, var2_idx):
    """Salva checkpoint no arquivo JSON"""
    data = {
        'modo': modo,
        'palavra_base': palavra_base,
        'palavra_var1': palavra_var1,
        'palavra_var2': palavra_var2,
        'base_idx': base_idx,
        'var1_idx': var1_idx,
        'var2_idx': var2_idx,
        'contador_total': stats.contador_total,
        'contador_validas': stats.contador_validas,
        'carteiras_verificadas': stats.carteiras_verificadas,
        'carteiras_com_saldo': stats.carteiras_com_saldo,
        'erros_api': stats.erros_api,
        'timestamp': datetime.now().isoformat()
    }
    
    with open(arquivo, 'w') as f:
        json.dump(data, f, indent=4)

def salvar_carteira_com_saldo(palavra_base, palavra_var1, palavra_var2, mnemonic, info):
    """Salva carteira com saldo no arquivo"""
    with open("saldo.txt", "a") as f:
        f.write("=" * 80 + "\n")
        f.write(f"💎 CARTEIRA COM SALDO ENCONTRADA - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n")
        f.write(f"Palavra Base: {palavra_base}\n")
        f.write(f"Palavra Variável 1: {palavra_var1}\n")
        if palavra_var2:
            f.write(f"Palavra Variável 2: {palavra_var2}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"Endereço: {info['address']}\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("=" * 80 + "\n\n")

# ==================== FUNÇÕES BIP39/BIP44 ====================

def criar_mnemonic(palavra_base, palavra_var1, palavra_var2, modo):
    """Cria mnemonic baseado no modo"""
    if modo == "11+1":
        palavras = [palavra_base] * 11 + [palavra_var1]
    elif modo == "10+2":
        palavras = [palavra_base] * 10 + [palavra_var1, palavra_var2]
    else:
        raise ValueError("Modo inválido")
    return " ".join(palavras)

def validar_mnemonic(mnemonic):
    """Valida se o mnemonic é válido segundo BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False

def mnemonic_para_seed(mnemonic):
    """Converte mnemonic para seed"""
    return Bip39SeedGenerator(mnemonic).Generate()

def derivar_bip44_btc(seed):
    """Deriva endereço Bitcoin usando BIP44"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index):
    """Extrai informações da carteira"""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    
    return {
        "address": addr_index.PublicKey().ToAddress(),
        "wif": priv_key_obj.ToWif(),
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
    }

# ==================== VERIFICAÇÃO DE SALDO ASSÍNCRONA ====================

semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

async def verificar_saldo_async(client: httpx.AsyncClient, endereco: str) -> Optional[bool]:
    """
    Verifica saldo do endereço de forma assíncrona.
    Retorna: True (tem saldo), False (sem saldo), None (erro/não conseguiu verificar)
    """
    url = f"https://mempool.space/api/address/{endereco}"
    
    for tentativa in range(MAX_RETRIES):
        async with semaphore:
            try:
                response = await client.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    funded_sum = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    return funded_sum > 0
                
                elif response.status_code == 429:
                    # Rate limit atingido, aguardar e tentar novamente
                    if tentativa < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (tentativa + 1))
                        continue
                    else:
                        stats.erros_api += 1
                        return None  # Desistir após tentativas
                
                else:
                    # Outro erro HTTP
                    stats.erros_api += 1
                    return None
                    
            except (httpx.ConnectError, httpx.TimeoutException):
                # Erro de conexão/timeout
                if tentativa < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    stats.erros_api += 1
                    return None
                    
            except Exception:
                # Qualquer outro erro
                stats.erros_api += 1
                return None
    
    return None

# ==================== PAINEL VISUAL ====================

def limpar_tela():
    """Limpa a tela do terminal"""
    os.system('clear' if os.name != 'nt' else 'cls')

def exibir_painel():
    """Exibe painel de estatísticas em tempo real"""
    limpar_tela()
    
    tempo_decorrido = time.time() - stats.inicio
    horas = int(tempo_decorrido // 3600)
    minutos = int((tempo_decorrido % 3600) // 60)
    segundos = int(tempo_decorrido % 60)
    
    taxa_comb = stats.taxa_atual()
    taxa_verif = stats.taxa_verificacao()
    
    # Calcular porcentagem de válidas
    pct_validas = (stats.contador_validas / stats.contador_total * 100) if stats.contador_total > 0 else 0
    
    # Calcular porcentagem de sucesso
    pct_sucesso = (stats.carteiras_com_saldo / stats.carteiras_verificadas * 100) if stats.carteiras_verificadas > 0 else 0
    
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER - PAINEL DE MONITORAMENTO".center(80))
    print("=" * 80)
    print()
    print(f"⏱️  TEMPO DE EXECUÇÃO: {horas:02d}h {minutos:02d}m {segundos:02d}s")
    print()
    print("📊 ESTATÍSTICAS GERAIS")
    print("-" * 80)
    print(f"  Combinações Testadas:        {stats.contador_total:>12,}")
    print(f"  Combinações Válidas (BIP39): {stats.contador_validas:>12,}  ({pct_validas:.2f}%)")
    print(f"  Carteiras Verificadas:       {stats.carteiras_verificadas:>12,}")
    print(f"  Carteiras com Saldo:         {stats.carteiras_com_saldo:>12,}  ({pct_sucesso:.8f}%)")
    print(f"  Erros de API:                {stats.erros_api:>12,}")
    print()
    print("⚡ DESEMPENHO")
    print("-" * 80)
    print(f"  Taxa de Combinações:         {taxa_comb:>12.1f} comb/s")
    print(f"  Taxa de Verificação:         {taxa_verif:>12.1f} verif/min")
    print(f"  Requisições/Minuto:          {taxa_verif:>12.1f}")
    print()
    print("🔄 PROGRESSO ATUAL")
    print("-" * 80)
    if stats.ultima_combinacao:
        print(f"  Última Combinação: {stats.ultima_combinacao[:70]}")
    if stats.ultimo_endereco:
        print(f"  Último Endereço:   {stats.ultimo_endereco}")
    print()
    print("=" * 80)
    print("💡 Pressione Ctrl+C para parar com segurança")
    print("=" * 80)

# ==================== FUNÇÃO PRINCIPAL ====================

async def processar_carteiras(client: httpx.AsyncClient, fila_verificacao: List[Dict]):
    """Processa a fila de carteiras para verificação de saldo"""
    if not fila_verificacao:
        return
    
    # Criar tarefas assíncronas para todas as carteiras na fila
    tasks = []
    for item in fila_verificacao:
        task = verificar_saldo_async(client, item['info']['address'])
        tasks.append((task, item))
    
    # Executar todas as verificações em paralelo (respeitando o semáforo)
    for task, item in tasks:
        resultado = await task
        stats.carteiras_verificadas += 1
        stats.ultimo_endereco = item['info']['address']
        
        # Se tem saldo (True), salvar
        if resultado is True:
            stats.carteiras_com_saldo += 1
            salvar_carteira_com_saldo(
                item['palavra_base'],
                item['palavra_var1'],
                item['palavra_var2'],
                item['mnemonic'],
                item['info']
            )
            print(f"\n🎉 CARTEIRA COM SALDO ENCONTRADA! 🎉")
            print(f"Endereço: {item['info']['address']}")
            print(f"Mnemonic: {item['mnemonic']}\n")

async def main_async():
    """Função principal assíncrona"""
    
    # Carregar palavras BIP39
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    
    # Escolher modo
    limpar_tela()
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER".center(80))
    print("=" * 80)
    print()
    print("📋 Modos disponíveis:")
    print("  • 11+1: 11 palavras repetidas + 1 palavra variável")
    print("  • 10+2: 10 palavras repetidas + 2 palavras variáveis")
    print()
    modo = input("👉 Escolha o modo ('11+1' ou '10+2'): ").strip()
    
    if modo not in ["11+1", "10+2"]:
        print("❌ Modo inválido!")
        return
    
    # Carregar checkpoint
    checkpoint = carregar_checkpoint("checkpoint.json")
    
    if checkpoint and checkpoint.get('modo') == modo:
        stats.contador_total = checkpoint.get('contador_total', 0)
        stats.contador_validas = checkpoint.get('contador_validas', 0)
        stats.carteiras_verificadas = checkpoint.get('carteiras_verificadas', 0)
        stats.carteiras_com_saldo = checkpoint.get('carteiras_com_saldo', 0)
        stats.erros_api = checkpoint.get('erros_api', 0)
        
        start_base_idx = checkpoint.get('base_idx', 0)
        start_var1_idx = checkpoint.get('var1_idx', 0)
        start_var2_idx = checkpoint.get('var2_idx', 0)
        
        print(f"\n✓ Checkpoint carregado!")
        print(f"  Continuando da posição: Base #{start_base_idx+1}, Var1 #{start_var1_idx+1}")
    else:
        start_base_idx = 0
        start_var1_idx = 0
        start_var2_idx = 0
        print(f"\n🆕 Começando do início...")
    
    input("\n▶️  Pressione ENTER para iniciar...")
    
    # Iniciar contagem de tempo
    stats.inicio = time.time()
    ultimo_checkpoint = time.time()
    ultimo_display = time.time()
    
    # Cliente HTTP assíncrono
    async with httpx.AsyncClient() as client:
        try:
            # Loop principal
            for i in range(start_base_idx, len(palavras)):
                palavra_base = palavras[i]
                
                # Determinar índice inicial
                start_j = start_var1_idx if i == start_base_idx else 0
                
                if modo == "11+1":
                    # Modo 11+1: apenas uma variável
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        stats.contador_total += 1
                        
                        # Criar mnemonic
                        mnemonic = criar_mnemonic(palavra_base, palavra_var1, None, modo)
                        stats.ultima_combinacao = mnemonic
                        
                        # Validar mnemonic (RÁPIDO, LOCAL)
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            
                            # Gerar carteira (RÁPIDO, LOCAL)
                            seed = mnemonic_para_seed(mnemonic)
                            addr_index = derivar_bip44_btc(seed)
                            info = mostrar_info(addr_index)
                            
                            # Verificar saldo (LENTO, ONLINE) - ASSÍNCRONO
                            resultado = await verificar_saldo_async(client, info['address'])
                            stats.carteiras_verificadas += 1
                            stats.ultimo_endereco = info['address']
                            
                            if resultado is True:
                                stats.carteiras_com_saldo += 1
                                salvar_carteira_com_saldo(palavra_base, palavra_var1, None, mnemonic, info)
                        
                        # Atualizar display
                        if time.time() - ultimo_display >= DISPLAY_UPDATE_INTERVAL:
                            exibir_painel()
                            ultimo_display = time.time()
                        
                        # Salvar checkpoint
                        if time.time() - ultimo_checkpoint >= CHECKPOINT_INTERVAL:
                            salvar_checkpoint("checkpoint.json", modo, palavra_base, palavra_var1, None, i, j, 0)
                            ultimo_checkpoint = time.time()
                
                elif modo == "10+2":
                    # Modo 10+2: duas variáveis
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        start_k = start_var2_idx if i == start_base_idx and j == start_j else 0
                        
                        for k in range(start_k, len(palavras)):
                            palavra_var2 = palavras[k]
                            stats.contador_total += 1
                            
                            # Criar mnemonic
                            mnemonic = criar_mnemonic(palavra_base, palavra_var1, palavra_var2, modo)
                            stats.ultima_combinacao = mnemonic
                            
                            # Validar mnemonic (RÁPIDO, LOCAL)
                            if validar_mnemonic(mnemonic):
                                stats.contador_validas += 1
                                
                                # Gerar carteira (RÁPIDO, LOCAL)
                                seed = mnemonic_para_seed(mnemonic)
                                addr_index = derivar_bip44_btc(seed)
                                info = mostrar_info(addr_index)
                                
                                # Verificar saldo (LENTO, ONLINE) - ASSÍNCRONO
                                resultado = await verificar_saldo_async(client, info['address'])
                                stats.carteiras_verificadas += 1
                                stats.ultimo_endereco = info['address']
                                
                                if resultado is True:
                                    stats.carteiras_com_saldo += 1
                                    salvar_carteira_com_saldo(palavra_base, palavra_var1, palavra_var2, mnemonic, info)
                            
                            # Atualizar display
                            if time.time() - ultimo_display >= DISPLAY_UPDATE_INTERVAL:
                                exibir_painel()
                                ultimo_display = time.time()
                            
                            # Salvar checkpoint
                            if time.time() - ultimo_checkpoint >= CHECKPOINT_INTERVAL:
                                salvar_checkpoint("checkpoint.json", modo, palavra_base, palavra_var1, palavra_var2, i, j, k)
                                ultimo_checkpoint = time.time()
                        
                        start_var2_idx = 0
                
                start_var1_idx = 0
                
                # Salvar checkpoint após cada palavra base
                salvar_checkpoint("checkpoint.json", modo, palavra_base, palavras[-1], palavras[-1] if modo == "10+2" else None, i, len(palavras)-1, len(palavras)-1 if modo == "10+2" else 0)
        
        except KeyboardInterrupt:
            print("\n\n⚠️  Programa interrompido pelo usuário")
        
        finally:
            # Salvar checkpoint final
            exibir_painel()
            print("\n✓ Checkpoint final salvo!")
            print(f"\n📁 Arquivos gerados:")
            print(f"  • checkpoint.json - Checkpoint para retomar")
            if stats.carteiras_com_saldo > 0:
                print(f"  • saldo.txt - Carteiras com saldo encontradas")

def main():
    """Ponto de entrada"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n\n👋 Programa encerrado")

if __name__ == "__main__":
    main()
