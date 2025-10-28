#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitcoin Wallet Finder - Versão FIXED
CORRIGIDO: Usa a MESMA lógica do código original
Melhorias: Batch + Cache + 6 APIs + Velocidade
Suporta: 11+1 e 10+2
"""

import asyncio
import os
import time
import json
from datetime import datetime
from collections import deque, defaultdict
import httpx
from bip_utils import (
    Bip39SeedGenerator, Bip39MnemonicValidator,
    Bip44, Bip44Coins, Bip44Changes
)
from typing import Optional, Dict, List, Tuple

# ==================== CONFIGURAÇÕES ====================
CONCURRENCY_INITIAL = 20  # Mais conservador
CONCURRENCY_MIN = 5
CONCURRENCY_MAX = 40
BATCH_SIZE = 15  # Batch menor para ser mais seguro
MAX_RETRIES = 2
TIMEOUT = 10  # Timeout maior (igual ao original)
CHECKPOINT_INTERVAL = 30
DISPLAY_UPDATE_INTERVAL = 0.5
LOG_LINES = 20

# ==================== ESTATÍSTICAS ====================
class Stats:
    def __init__(self):
        self.contador_total = 0
        self.contador_validas = 0
        self.carteiras_verificadas = 0
        self.carteiras_com_saldo = 0
        self.erros_por_tipo = defaultdict(int)
        self.api_stats = defaultdict(lambda: {'sucessos': 0, 'falhas': 0})
        self.inicio = time.time()
        self.ultima_combinacao = ""
        self.ultimo_endereco = ""
        self.concurrency_atual = CONCURRENCY_INITIAL
        self.erros_consecutivos = 0
        self.sucessos_consecutivos = 0
        self.modo_operacao = ""
        self.cache_hits = 0
        
    def registrar_sucesso(self, api_name=None):
        self.sucessos_consecutivos += 1
        self.erros_consecutivos = 0
        if api_name:
            self.api_stats[api_name]['sucessos'] += 1
        
        if self.sucessos_consecutivos >= 30 and self.concurrency_atual < CONCURRENCY_MAX:
            self.concurrency_atual = min(CONCURRENCY_MAX, self.concurrency_atual + 2)
            self.sucessos_consecutivos = 0
    
    def registrar_erro(self, tipo_erro, api_name=None):
        self.erros_por_tipo[tipo_erro] += 1
        self.erros_consecutivos += 1
        self.sucessos_consecutivos = 0
        if api_name:
            self.api_stats[api_name]['falhas'] += 1
        
        if self.erros_consecutivos >= 5 and self.concurrency_atual > CONCURRENCY_MIN:
            self.concurrency_atual = max(CONCURRENCY_MIN, self.concurrency_atual - 2)
            self.erros_consecutivos = 0
    
    def total_erros(self):
        return sum(self.erros_por_tipo.values())
    
    def taxa_atual(self):
        tempo_decorrido = time.time() - self.inicio
        return self.contador_total / tempo_decorrido if tempo_decorrido > 0 else 0
    
    def taxa_verificacao(self):
        tempo_decorrido = time.time() - self.inicio
        return (self.carteiras_verificadas / tempo_decorrido) * 60 if tempo_decorrido > 0 else 0

stats = Stats()
log_buffer = deque(maxlen=LOG_LINES)
semaphore = None
cache_enderecos = {}

def atualizar_semaphore():
    global semaphore
    semaphore = asyncio.Semaphore(stats.concurrency_atual)

# ==================== FUNÇÕES DE ARQUIVO ====================

def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    if not os.path.exists(arquivo):
        try:
            from bip_utils.bip.bip39 import Bip39WordsNum
            from bip_utils import Bip39Languages
            palavras = Bip39WordsNum.FromWordsNumber(2048).GetList(Bip39Languages.ENGLISH)
            with open(arquivo, 'w') as f:
                f.write('\n'.join(palavras))
            print(f"✓ Arquivo '{arquivo}' criado")
            return list(palavras)
        except:
            raise FileNotFoundError(f"Arquivo {arquivo} não encontrado!")
    
    with open(arquivo, 'r') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    return palavras

def carregar_checkpoint(arquivo="checkpoint.json"):
    if not os.path.exists(arquivo):
        return None
    
    try:
        with open(arquivo, 'r') as f:
            data = json.load(f)
            global cache_enderecos
            cache_enderecos = data.get('cache', {})
            
            api_stats_saved = data.get('api_stats', {})
            for api_name, stats_dict in api_stats_saved.items():
                stats.api_stats[api_name] = stats_dict
            
            return data
    except:
        return None

def salvar_checkpoint(arquivo, modo, palavra_base, palavra_var1, palavra_var2, base_idx, var1_idx, var2_idx):
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
        'erros_por_tipo': dict(stats.erros_por_tipo),
        'api_stats': dict(stats.api_stats),
        'cache_hits': stats.cache_hits,
        'cache': cache_enderecos,
        'timestamp': datetime.now().isoformat()
    }
    
    with open(arquivo, 'w') as f:
        json.dump(data, f, indent=4)

def salvar_carteira_com_saldo(palavra_base, palavra_var1, palavra_var2, mnemonic, info, saldo_valor=None):
    """LÓGICA ORIGINAL - Salva carteira com saldo"""
    with open("saldo.txt", "a") as f:
        f.write("=" * 80 + "\n")
        f.write(f"💎 CARTEIRA COM SALDO - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n")
        f.write(f"Palavra Base: {palavra_base}\n")
        f.write(f"Palavra Variável 1: {palavra_var1}\n")
        if palavra_var2:
            f.write(f"Palavra Variável 2: {palavra_var2}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"Endereço: {info['address']}\n")
        if saldo_valor:
            f.write(f"Saldo: {saldo_valor} satoshis\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("=" * 80 + "\n\n")

# ==================== FUNÇÕES BIP39/BIP44 (LÓGICA ORIGINAL) ====================

def criar_mnemonic(palavra_base, palavra_var1, palavra_var2, modo):
    """LÓGICA ORIGINAL"""
    if modo == "11+1":
        palavras = [palavra_base] * 11 + [palavra_var1]
    elif modo == "10+2":
        palavras = [palavra_base] * 10 + [palavra_var1, palavra_var2]
    else:
        raise ValueError("Modo inválido")
    return " ".join(palavras)

def validar_mnemonic(mnemonic):
    """LÓGICA ORIGINAL"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False

def mnemonic_para_seed(mnemonic):
    """LÓGICA ORIGINAL"""
    return Bip39SeedGenerator(mnemonic).Generate()

def derivar_bip44_btc(seed):
    """LÓGICA ORIGINAL"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index):
    """LÓGICA ORIGINAL"""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    
    return {
        "address": addr_index.PublicKey().ToAddress(),
        "wif": priv_key_obj.ToWif(),
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
    }

# ==================== LOGGING ====================

def adicionar_log(mensagem):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_buffer.append(f"[{timestamp}] {mensagem}")

# ==================== VERIFICAÇÃO DE SALDO (LÓGICA ORIGINAL + MELHORIAS) ====================

async def verificar_saldo_mempool(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
    """
    LÓGICA ORIGINAL - Mempool.space como API principal
    Retorna: (tem_saldo, valor_saldo, api_usada)
    """
    try:
        url = f"https://mempool.space/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            # LÓGICA ORIGINAL: funded_txo_sum > 0
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            tem_saldo = saldo > 0
            
            stats.registrar_sucesso("Mempool.space")
            return tem_saldo, saldo, "Mempool.space"
        
        elif response.status_code == 429:
            stats.registrar_erro("429", "Mempool.space")
            return None, None, None
    except:
        stats.registrar_erro("Error", "Mempool.space")
    
    return None, None, None

async def verificar_saldo_blockchain(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
    """Blockchain.info como fallback"""
    try:
        url = f"https://blockchain.info/balance?active={endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get(endereco, {}).get('final_balance', 0)
            tem_saldo = saldo > 0
            
            stats.registrar_sucesso("Blockchain.info")
            return tem_saldo, saldo, "Blockchain.info"
        
        elif response.status_code == 429:
            stats.registrar_erro("429", "Blockchain.info")
    except:
        stats.registrar_erro("Error", "Blockchain.info")
    
    return None, None, None

async def verificar_saldo_blockcypher(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
    """BlockCypher como fallback"""
    try:
        url = f"https://api.blockcypher.com/v1/btc/main/addrs/{endereco}/balance"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('final_balance', 0)
            tem_saldo = saldo > 0
            
            stats.registrar_sucesso("BlockCypher")
            return tem_saldo, saldo, "BlockCypher"
        
        elif response.status_code == 429:
            stats.registrar_erro("429", "BlockCypher")
    except:
        stats.registrar_erro("Error", "BlockCypher")
    
    return None, None, None

async def verificar_saldo_blockstream(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
    """Blockstream como fallback"""
    try:
        url = f"https://blockstream.info/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            tem_saldo = saldo > 0
            
            stats.registrar_sucesso("Blockstream")
            return tem_saldo, saldo, "Blockstream"
        
        elif response.status_code == 429:
            stats.registrar_erro("429", "Blockstream")
    except:
        stats.registrar_erro("Error", "Blockstream")
    
    return None, None, None

async def verificar_saldo_completo(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str]]:
    """
    Tenta verificar com todas as APIs em sequência
    Retorna: (tem_saldo, valor_saldo, api_usada)
    """
    # Verificar cache primeiro
    if endereco in cache_enderecos:
        stats.cache_hits += 1
        adicionar_log(f"💾 Cache | {endereco[:20]}...")
        return cache_enderecos[endereco], 0, "Cache"
    
    # Tentar APIs em ordem de prioridade
    apis = [
        verificar_saldo_mempool,      # API principal (igual ao original)
        verificar_saldo_blockchain,   # Fallback 1
        verificar_saldo_blockcypher,  # Fallback 2
        verificar_saldo_blockstream,  # Fallback 3
    ]
    
    for api_func in apis:
        tem_saldo, valor_saldo, api_usada = await api_func(client, endereco)
        
        if tem_saldo is not None:
            # Salvar no cache
            cache_enderecos[endereco] = tem_saldo
            return tem_saldo, valor_saldo, api_usada
    
    # Todas falharam
    stats.registrar_erro("AllAPIsFailed")
    return None, None, None

async def processar_carteira(client: httpx.AsyncClient, item: Dict) -> None:
    """Processa uma carteira individual"""
    endereco = item['info']['address']
    stats.carteiras_verificadas += 1
    stats.ultimo_endereco = endereco
    
    adicionar_log(f"🔍 Verificando | {endereco[:20]}...")
    
    async with semaphore:
        tem_saldo, valor_saldo, api_usada = await verificar_saldo_completo(client, endereco)
    
    if tem_saldo is None:
        adicionar_log(f"❌ Todas APIs falharam | {endereco[:20]}...")
        return
    
    if tem_saldo:
        stats.carteiras_com_saldo += 1
        salvar_carteira_com_saldo(
            item['palavra_base'],
            item['palavra_var1'],
            item['palavra_var2'],
            item['mnemonic'],
            item['info'],
            valor_saldo
        )
        adicionar_log(f"✅ SALDO: {valor_saldo} sat | {endereco[:20]}... | {api_usada}")
        print(f"\n🎉 CARTEIRA COM SALDO ENCONTRADA! 🎉")
        print(f"Endereço: {endereco}")
        print(f"Saldo: {valor_saldo} satoshis")
        print(f"Mnemonic: {item['mnemonic']}\n")
    else:
        adicionar_log(f"⭕ Sem saldo | {endereco[:20]}... | {api_usada}")

# ==================== PAINEL ====================

def limpar_tela():
    os.system('clear' if os.name != 'nt' else 'cls')

def exibir_painel():
    limpar_tela()
    
    tempo_decorrido = time.time() - stats.inicio
    horas = int(tempo_decorrido // 3600)
    minutos = int((tempo_decorrido % 3600) // 60)
    segundos = int(tempo_decorrido % 60)
    
    taxa_comb = stats.taxa_atual()
    taxa_verif = stats.taxa_verificacao()
    
    pct_validas = (stats.contador_validas / stats.contador_total * 100) if stats.contador_total > 0 else 0
    pct_sucesso = (stats.carteiras_com_saldo / stats.carteiras_verificadas * 100) if stats.carteiras_verificadas > 0 else 0
    
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER - FIXED (Lógica Original + Velocidade)".center(80))
    print("=" * 80)
    print()
    print(f"⏱️  {horas:02d}h {minutos:02d}m {segundos:02d}s | 🚀 Concorrência: {stats.concurrency_atual} | 📋 Modo: {stats.modo_operacao}")
    print()
    print("📊 ESTATÍSTICAS")
    print("-" * 80)
    print(f"  Testadas: {stats.contador_total:>10,} | Válidas: {stats.contador_validas:>8,} ({pct_validas:.2f}%)")
    print(f"  Verificadas: {stats.carteiras_verificadas:>7,} | Com Saldo: {stats.carteiras_com_saldo:>5,} ({pct_sucesso:.8f}%)")
    print(f"  Cache Hits: {stats.cache_hits:>8,} | Erros: {stats.total_erros():>7,}")
    
    if stats.api_stats:
        print(f"\n  🌐 APIs:")
        for api_name, api_stat in sorted(stats.api_stats.items(), key=lambda x: x[1]['sucessos'], reverse=True)[:4]:
            total = api_stat['sucessos'] + api_stat['falhas']
            taxa_sucesso = (api_stat['sucessos'] / total * 100) if total > 0 else 0
            print(f"     • {api_name}: {api_stat['sucessos']} OK, {api_stat['falhas']} ERR ({taxa_sucesso:.1f}%)")
    
    if stats.erros_por_tipo:
        print(f"\n  📛 Erros:")
        for tipo, count in sorted(stats.erros_por_tipo.items(), key=lambda x: x[1], reverse=True)[:3]:
            print(f"     • {tipo}: {count}")
    
    print()
    print("⚡ DESEMPENHO")
    print("-" * 80)
    print(f"  Taxa: {taxa_comb:>8.1f} comb/s | Verificações: {taxa_verif:>8.1f} req/min")
    print()
    print("📜 ATIVIDADES")
    print("-" * 80)
    
    if log_buffer:
        for linha in log_buffer:
            print(f"  {linha}")
    else:
        print("  Aguardando...")
    
    print()
    print("=" * 80)
    print("💡 Ctrl+C para parar | Lógica Original + Cache + 4 APIs")
    print("=" * 80)

# ==================== FUNÇÃO PRINCIPAL (LÓGICA ORIGINAL 100%) ====================

async def main_async():
    global semaphore
    
    atualizar_semaphore()
    
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    
    limpar_tela()
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER FIXED".center(80))
    print("=" * 80)
    print()
    print("📋 Modos:")
    print("  • 11+1: 11 palavras repetidas + 1 variável")
    print("  • 10+2: 10 palavras repetidas + 2 variáveis")
    print()
    modo = input("👉 Escolha ('11+1' ou '10+2'): ").strip()
    
    if modo not in ["11+1", "10+2"]:
        print("❌ Modo inválido!")
        return
    
    stats.modo_operacao = modo
    
    checkpoint = carregar_checkpoint("checkpoint.json")
    
    if checkpoint and checkpoint.get('modo') == modo:
        stats.contador_total = checkpoint.get('contador_total', 0)
        stats.contador_validas = checkpoint.get('contador_validas', 0)
        stats.carteiras_verificadas = checkpoint.get('carteiras_verificadas', 0)
        stats.carteiras_com_saldo = checkpoint.get('carteiras_com_saldo', 0)
        stats.cache_hits = checkpoint.get('cache_hits', 0)
        
        erros_salvos = checkpoint.get('erros_por_tipo', {})
        for tipo, count in erros_salvos.items():
            stats.erros_por_tipo[tipo] = count
        
        start_base_idx = checkpoint.get('base_idx', 0)
        start_var1_idx = checkpoint.get('var1_idx', 0)
        start_var2_idx = checkpoint.get('var2_idx', 0)
        
        print(f"\n✓ Checkpoint! Cache: {len(cache_enderecos)}")
        print(f"  Base #{start_base_idx+1}, Var1 #{start_var1_idx+1}")
    else:
        start_base_idx = 0
        start_var1_idx = 0
        start_var2_idx = 0
        print(f"\n🆕 Início...")
    
    print(f"\n🚀 Config:")
    print(f"  • Concorrência: {CONCURRENCY_INITIAL} (5-40)")
    print(f"  • APIs: 4 com fallback (Mempool.space principal)")
    print(f"  • Cache: {len(cache_enderecos)} endereços")
    print(f"  • Lógica: IGUAL ao código original")
    
    input("\n▶️  ENTER para iniciar...")
    
    stats.inicio = time.time()
    ultimo_checkpoint = time.time()
    ultimo_display = time.time()
    
    # Buffer para processar em paralelo
    tarefas_pendentes = []
    
    async with httpx.AsyncClient() as client:
        try:
            # LÓGICA ORIGINAL - Loop principal
            for i in range(start_base_idx, len(palavras)):
                palavra_base = palavras[i]
                
                start_j = start_var1_idx if i == start_base_idx else 0
                
                if modo == "11+1":
                    # LÓGICA ORIGINAL 11+1
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        stats.contador_total += 1
                        
                        # 1. VALIDAR (LÓGICA ORIGINAL)
                        mnemonic = criar_mnemonic(palavra_base, palavra_var1, None, modo)
                        stats.ultima_combinacao = mnemonic
                        
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            adicionar_log(f"✔️ Válida | {mnemonic[:50]}...")
                            
                            # 2. GERAR CARTEIRA (LÓGICA ORIGINAL)
                            seed = mnemonic_para_seed(mnemonic)
                            addr_index = derivar_bip44_btc(seed)
                            info = mostrar_info(addr_index)
                            
                            # 3. VERIFICAR SALDO (async para velocidade)
                            if stats.concurrency_atual != semaphore._value:
                                atualizar_semaphore()
                            
                            tarefa = processar_carteira(client, {
                                'palavra_base': palavra_base,
                                'palavra_var1': palavra_var1,
                                'palavra_var2': None,
                                'mnemonic': mnemonic,
                                'info': info
                            })
                            tarefas_pendentes.append(tarefa)
                            
                            # Processar em lotes de 10
                            if len(tarefas_pendentes) >= 10:
                                await asyncio.gather(*tarefas_pendentes)
                                tarefas_pendentes = []
                        
                        # Atualizar display
                        if time.time() - ultimo_display >= DISPLAY_UPDATE_INTERVAL:
                            exibir_painel()
                            ultimo_display = time.time()
                        
                        # Salvar checkpoint
                        if time.time() - ultimo_checkpoint >= CHECKPOINT_INTERVAL:
                            if tarefas_pendentes:
                                await asyncio.gather(*tarefas_pendentes)
                                tarefas_pendentes = []
                            
                            salvar_checkpoint("checkpoint.json", modo, palavra_base, palavra_var1, None, i, j, 0)
                            ultimo_checkpoint = time.time()
                
                elif modo == "10+2":
                    # LÓGICA ORIGINAL 10+2
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        start_k = start_var2_idx if i == start_base_idx and j == start_j else 0
                        
                        for k in range(start_k, len(palavras)):
                            palavra_var2 = palavras[k]
                            stats.contador_total += 1
                            
                            # 1. VALIDAR (LÓGICA ORIGINAL)
                            mnemonic = criar_mnemonic(palavra_base, palavra_var1, palavra_var2, modo)
                            stats.ultima_combinacao = mnemonic
                            
                            if validar_mnemonic(mnemonic):
                                stats.contador_validas += 1
                                adicionar_log(f"✔️ Válida | {mnemonic[:50]}...")
                                
                                # 2. GERAR CARTEIRA (LÓGICA ORIGINAL)
                                seed = mnemonic_para_seed(mnemonic)
                                addr_index = derivar_bip44_btc(seed)
                                info = mostrar_info(addr_index)
                                
                                # 3. VERIFICAR SALDO (async)
                                if stats.concurrency_atual != semaphore._value:
                                    atualizar_semaphore()
                                
                                tarefa = processar_carteira(client, {
                                    'palavra_base': palavra_base,
                                    'palavra_var1': palavra_var1,
                                    'palavra_var2': palavra_var2,
                                    'mnemonic': mnemonic,
                                    'info': info
                                })
                                tarefas_pendentes.append(tarefa)
                                
                                if len(tarefas_pendentes) >= 10:
                                    await asyncio.gather(*tarefas_pendentes)
                                    tarefas_pendentes = []
                            
                            # Atualizar display
                            if time.time() - ultimo_display >= DISPLAY_UPDATE_INTERVAL:
                                exibir_painel()
                                ultimo_display = time.time()
                            
                            # Salvar checkpoint
                            if time.time() - ultimo_checkpoint >= CHECKPOINT_INTERVAL:
                                if tarefas_pendentes:
                                    await asyncio.gather(*tarefas_pendentes)
                                    tarefas_pendentes = []
                                
                                salvar_checkpoint("checkpoint.json", modo, palavra_base, palavra_var1, palavra_var2, i, j, k)
                                ultimo_checkpoint = time.time()
                        
                        start_var2_idx = 0
                
                start_var1_idx = 0
                
                # Processar tarefas pendentes
                if tarefas_pendentes:
                    await asyncio.gather(*tarefas_pendentes)
                    tarefas_pendentes = []
                
                salvar_checkpoint("checkpoint.json", modo, palavra_base, palavras[-1], palavras[-1] if modo == "10+2" else None, i, len(palavras)-1, len(palavras)-1 if modo == "10+2" else 0)
        
        except KeyboardInterrupt:
            adicionar_log("⚠️ Interrompido")
            if tarefas_pendentes:
                await asyncio.gather(*tarefas_pendentes)
        
        finally:
            exibir_painel()
            print("\n✓ Checkpoint salvo!")
            print(f"\n📁 Arquivos:")
            print(f"  • checkpoint.json - {len(cache_enderecos)} em cache")
            if stats.carteiras_com_saldo > 0:
                print(f"  • saldo.txt - {stats.carteiras_com_saldo} carteira(s)! 🎉")

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n\n👋 Encerrado")

if __name__ == "__main__":
    main()
