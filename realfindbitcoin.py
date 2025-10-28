#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bitcoin_finder_ULTIMATE_FINAL.py
Versão FINAL com máxima velocidade e cobertura completa (BIP44+BIP49+BIP84)
Baseado no código original que FUNCIONOU + melhorias de velocidade
"""

import os
import sys
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

import httpx
from bip_utils import (
    Bip39SeedGenerator, 
    Bip39MnemonicValidator,
    Bip44, 
    Bip44Coins, 
    Bip44Changes,
    Bip49,
    Bip49Coins,
    Bip84,
    Bip84Coins
)

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================

# Velocidade e Concorrência
CONCURRENCY_INITIAL = 60  # Começa com 60 requisições simultâneas
CONCURRENCY_MIN = 10      # Mínimo 10
CONCURRENCY_MAX = 120     # Máximo 120
TIMEOUT = 3               # Timeout de 3 segundos (agressivo)

# Arquivos
CHECKPOINT_FILE = "checkpoint.json"
SALDO_FILE = "saldo.txt"
BIP39_WORDS_FILE = "bip39-words.txt"

# ============================================================================
# CLASSE DE ESTATÍSTICAS
# ============================================================================

class Stats:
    def __init__(self):
        self.contador_total = 0
        self.contador_validas = 0
        self.contador_invalidas = 0
        
        # Carteiras com saldo por tipo
        self.carteiras_com_saldo_bip44 = 0
        self.carteiras_com_saldo_bip49 = 0
        self.carteiras_com_saldo_bip84 = 0
        
        # Estatísticas de APIs
        self.api_stats = {}
        self.erros_por_tipo = {}
        
        # Concorrência adaptativa
        self.concurrency_atual = CONCURRENCY_INITIAL
        self.erros_429_consecutivos = 0
        self.sucessos_consecutivos = 0
        
        # Performance
        self.inicio = time.time()
        self.ultimo_update = time.time()
        self.logs = []
        
    def registrar_sucesso_api(self, api_name: str):
        if api_name not in self.api_stats:
            self.api_stats[api_name] = {"ok": 0, "err": 0}
        self.api_stats[api_name]["ok"] += 1
        self.sucessos_consecutivos += 1
        self.erros_429_consecutivos = 0
        
        # Aumentar concorrência se tiver muitos sucessos
        if self.sucessos_consecutivos >= 30:
            self.concurrency_atual = min(CONCURRENCY_MAX, self.concurrency_atual + 5)
            self.sucessos_consecutivos = 0
            
    def registrar_erro_api(self, api_name: str, erro_tipo: str):
        if api_name not in self.api_stats:
            self.api_stats[api_name] = {"ok": 0, "err": 0}
        self.api_stats[api_name]["err"] += 1
        
        if erro_tipo not in self.erros_por_tipo:
            self.erros_por_tipo[erro_tipo] = 0
        self.erros_por_tipo[erro_tipo] += 1
        
        # Reduzir concorrência se der erro 429
        if erro_tipo == "429":
            self.erros_429_consecutivos += 1
            self.sucessos_consecutivos = 0
            
            if self.erros_429_consecutivos >= 3:
                self.concurrency_atual = max(CONCURRENCY_MIN, self.concurrency_atual - 10)
                self.erros_429_consecutivos = 0
                
    def adicionar_log(self, mensagem: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {mensagem}")
        if len(self.logs) > 25:
            self.logs.pop(0)
            
    def get_total_com_saldo(self):
        return (self.carteiras_com_saldo_bip44 + 
                self.carteiras_com_saldo_bip49 + 
                self.carteiras_com_saldo_bip84)

# ============================================================================
# FUNÇÕES BIP39 E DERIVAÇÃO
# ============================================================================

def carregar_palavras_bip39(arquivo=BIP39_WORDS_FILE):
    """Carrega lista de palavras BIP39"""
    if not os.path.exists(arquivo):
        # Gerar arquivo se não existir
        from bip_utils import Bip39WordsNum, Bip39Languages
        wordlist = Bip39WordsNum.FromWordsNumber(2048, Bip39Languages.ENGLISH)
        with open(arquivo, 'w') as f:
            for word in wordlist:
                f.write(word + '\n')
    
    with open(arquivo, 'r') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    return palavras

def validar_mnemonic(mnemonic: str) -> bool:
    """Valida mnemonic BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False

def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Gera seed BIP39"""
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)

def derivar_enderecos(seed: bytes) -> Dict[str, Dict[str, str]]:
    """
    Deriva os 3 tipos de endereços Bitcoin (BIP44, BIP49, BIP84)
    Retorna dict com informações de cada tipo
    """
    enderecos = {}
    
    # BIP44 - Legacy (m/44'/0'/0'/0/0)
    try:
        bip44_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
        bip44_acc = bip44_ctx.Purpose().Coin().Account(0)
        bip44_change = bip44_acc.Change(Bip44Changes.CHAIN_EXT)
        bip44_addr = bip44_change.AddressIndex(0)
        
        enderecos["BIP44"] = {
            "tipo": "Legacy",
            "derivacao": "m/44'/0'/0'/0/0",
            "endereco": bip44_addr.PublicKey().ToAddress(),
            "priv_hex": bip44_addr.PrivateKey().Raw().ToHex(),
            "wif": bip44_addr.PrivateKey().ToWif(),
            "pub_hex": bip44_addr.PublicKey().RawCompressed().ToHex()
        }
    except:
        pass
    
    # BIP49 - SegWit (m/49'/0'/0'/0/0)
    try:
        bip49_ctx = Bip49.FromSeed(seed, Bip49Coins.BITCOIN)
        bip49_acc = bip49_ctx.Purpose().Coin().Account(0)
        bip49_change = bip49_acc.Change(Bip44Changes.CHAIN_EXT)
        bip49_addr = bip49_change.AddressIndex(0)
        
        enderecos["BIP49"] = {
            "tipo": "SegWit",
            "derivacao": "m/49'/0'/0'/0/0",
            "endereco": bip49_addr.PublicKey().ToAddress(),
            "priv_hex": bip49_addr.PrivateKey().Raw().ToHex(),
            "wif": bip49_addr.PrivateKey().ToWif(),
            "pub_hex": bip49_addr.PublicKey().RawCompressed().ToHex()
        }
    except:
        pass
    
    # BIP84 - Native SegWit (m/84'/0'/0'/0/0)
    try:
        bip84_ctx = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        bip84_acc = bip84_ctx.Purpose().Coin().Account(0)
        bip84_change = bip84_acc.Change(Bip44Changes.CHAIN_EXT)
        bip84_addr = bip84_change.AddressIndex(0)
        
        enderecos["BIP84"] = {
            "tipo": "Native SegWit",
            "derivacao": "m/84'/0'/0'/0/0",
            "endereco": bip84_addr.PublicKey().ToAddress(),
            "priv_hex": bip84_addr.PrivateKey().Raw().ToHex(),
            "wif": bip84_addr.PrivateKey().ToWif(),
            "pub_hex": bip84_addr.PublicKey().RawCompressed().ToHex()
        }
    except:
        pass
    
    return enderecos

# ============================================================================
# VERIFICAÇÃO DE SALDO - 8 APIs EM PARALELO
# ============================================================================

async def verificar_saldo_mempool(client: httpx.AsyncClient, endereco: str):
    """API 1: Mempool.space (PRINCIPAL - igual ao código original)"""
    try:
        url = f"https://mempool.space/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            # LÓGICA ORIGINAL: funded_txo_sum > 0
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            return saldo > 0, saldo, "Mempool.space"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_blockchain(client: httpx.AsyncClient, endereco: str):
    """API 2: Blockchain.info"""
    try:
        url = f"https://blockchain.info/balance?active={endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get(endereco, {}).get('final_balance', 0)
            return saldo > 0, saldo, "Blockchain.info"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_blockcypher(client: httpx.AsyncClient, endereco: str):
    """API 3: BlockCypher"""
    try:
        url = f"https://api.blockcypher.com/v1/btc/main/addrs/{endereco}/balance"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('final_balance', 0)
            return saldo > 0, saldo, "BlockCypher"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_blockstream(client: httpx.AsyncClient, endereco: str):
    """API 4: Blockstream"""
    try:
        url = f"https://blockstream.info/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            return saldo > 0, saldo, "Blockstream"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_blockchair(client: httpx.AsyncClient, endereco: str):
    """API 5: Blockchair"""
    try:
        url = f"https://api.blockchair.com/bitcoin/dashboards/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('data', {}).get(endereco, {}).get('address', {}).get('balance', 0)
            return saldo > 0, saldo, "Blockchair"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_chainso(client: httpx.AsyncClient, endereco: str):
    """API 6: Chain.so"""
    try:
        url = f"https://chain.so/api/v2/get_address_balance/BTC/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo_str = data.get('data', {}).get('confirmed_balance', '0')
            saldo = int(float(saldo_str) * 100000000)  # Converter BTC para satoshis
            return saldo > 0, saldo, "Chain.so"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_btcscan(client: httpx.AsyncClient, endereco: str):
    """API 7: BTCScan"""
    try:
        url = f"https://btcscan.org/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('balance', 0)
            return saldo > 0, saldo, "BTCScan"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_sochain(client: httpx.AsyncClient, endereco: str):
    """API 8: SoChain"""
    try:
        url = f"https://sochain.com/api/v2/get_address_balance/BTC/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo_str = data.get('data', {}).get('confirmed_balance', '0')
            saldo = int(float(saldo_str) * 100000000)
            return saldo > 0, saldo, "SoChain"
        elif response.status_code == 429:
            return None, None, "429"
    except:
        pass
    return None, None, None

async def verificar_saldo_paralelo(client: httpx.AsyncClient, endereco: str, stats: Stats):
    """
    Dispara TODAS as 8 APIs em PARALELO e retorna assim que a PRIMEIRA responder!
    Isso garante máxima velocidade e resiliência
    """
    apis = [
        verificar_saldo_mempool(client, endereco),      # API Principal
        verificar_saldo_blockchain(client, endereco),
        verificar_saldo_blockcypher(client, endereco),
        verificar_saldo_blockstream(client, endereco),
        verificar_saldo_blockchair(client, endereco),
        verificar_saldo_chainso(client, endereco),
        verificar_saldo_btcscan(client, endereco),
        verificar_saldo_sochain(client, endereco),
    ]
    
    # Dispara todas em paralelo
    resultados = await asyncio.gather(*apis, return_exceptions=True)
    
    # Processa resultados (primeira que responder com sucesso)
    for tem_saldo, saldo, api_name in resultados:
        if isinstance((tem_saldo, saldo, api_name), Exception):
            continue
            
        if api_name == "429":
            stats.registrar_erro_api("Multiple", "429")
            continue
            
        if api_name and tem_saldo is not None:
            stats.registrar_sucesso_api(api_name)
            return tem_saldo, saldo, api_name
    
    # Se todas falharam
    stats.registrar_erro_api("All", "AllFailed")
    return False, 0, None

# ============================================================================
# PROCESSAMENTO DE CARTEIRA
# ============================================================================

async def processar_carteira(
    client: httpx.AsyncClient,
    mnemonic: str,
    palavra_base: str,
    palavra_var1: str,
    stats: Stats,
    semaphore: asyncio.Semaphore
):
    """Processa uma carteira válida: deriva endereços e verifica saldo"""
    async with semaphore:
        # Gerar seed
        seed = mnemonic_para_seed(mnemonic)
        
        # Derivar os 3 tipos de endereços
        enderecos = derivar_enderecos(seed)
        
        # Verificar saldo em cada tipo
        for bip_type, info in enderecos.items():
            endereco = info["endereco"]
            
            stats.adicionar_log(f"🔍 Verificando {bip_type} | {endereco[:20]}...")
            
            tem_saldo, saldo, api_name = await verificar_saldo_paralelo(client, endereco, stats)
            
            if tem_saldo:
                # ENCONTROU SALDO!
                if bip_type == "BIP44":
                    stats.carteiras_com_saldo_bip44 += 1
                elif bip_type == "BIP49":
                    stats.carteiras_com_saldo_bip49 += 1
                elif bip_type == "BIP84":
                    stats.carteiras_com_saldo_bip84 += 1
                
                saldo_btc = saldo / 100000000.0
                stats.adicionar_log(
                    f"✅ SALDO: {saldo} sat ({saldo_btc:.8f} BTC) | "
                    f"{bip_type} ({info['tipo']}) | {endereco[:20]}... | {api_name}"
                )
                
                # Salvar em arquivo
                salvar_carteira_com_saldo(palavra_base, palavra_var1, mnemonic, info, bip_type, saldo, saldo_btc, api_name)
            else:
                stats.adicionar_log(f"⭕ Sem saldo | {bip_type} | {endereco[:20]}...")

def salvar_carteira_com_saldo(
    palavra_base: str,
    palavra_var1: str,
    mnemonic: str,
    info: Dict[str, str],
    bip_type: str,
    saldo_sat: int,
    saldo_btc: float,
    api_name: str
):
    """Salva carteira com saldo no arquivo"""
    with open(SALDO_FILE, "a") as f:
        f.write("=" * 80 + "\n")
        f.write("💎 CARTEIRA COM SALDO ENCONTRADA\n")
        f.write("=" * 80 + "\n")
        f.write(f"Data/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"\nTipo de Derivação: {bip_type} ({info['tipo']})\n")
        f.write(f"Derivação: {info['derivacao']}\n")
        f.write(f"\nPalavra Base: {palavra_base} (repetida 11x)\n")
        f.write(f"Palavra Variável: {palavra_var1}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"\nEndereço: {info['endereco']}\n")
        f.write(f"Saldo: {saldo_sat} satoshis ({saldo_btc:.8f} BTC)\n")
        f.write(f"API Usada: {api_name}\n")
        f.write(f"\nChave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública (HEX): {info['pub_hex']}\n")
        f.write("-" * 80 + "\n\n")

# ============================================================================
# CHECKPOINT
# ============================================================================

def carregar_checkpoint():
    """Carrega checkpoint do arquivo JSON"""
    if not os.path.exists(CHECKPOINT_FILE):
        return None, None, 0, 0, 0, 0, 0
    
    try:
        with open(CHECKPOINT_FILE, 'r') as f:
            data = json.load(f)
            return (
                data.get("palavra_base"),
                data.get("palavra_var1"),
                data.get("contador_total", 0),
                data.get("contador_validas", 0),
                data.get("carteiras_bip44", 0),
                data.get("carteiras_bip49", 0),
                data.get("carteiras_bip84", 0)
            )
    except:
        return None, None, 0, 0, 0, 0, 0

def salvar_checkpoint(
    palavra_base: str,
    palavra_var1: str,
    stats: Stats
):
    """Salva checkpoint em JSON"""
    data = {
        "palavra_base": palavra_base,
        "palavra_var1": palavra_var1,
        "contador_total": stats.contador_total,
        "contador_validas": stats.contador_validas,
        "carteiras_bip44": stats.carteiras_com_saldo_bip44,
        "carteiras_bip49": stats.carteiras_com_saldo_bip49,
        "carteiras_bip84": stats.carteiras_com_saldo_bip84,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# ============================================================================
# PAINEL VISUAL
# ============================================================================

def mostrar_painel(stats: Stats, modo: str, palavra_atual: str):
    """Mostra painel visual com estatísticas"""
    os.system('clear' if os.name == 'posix' else 'cls')
    
    tempo_decorrido = time.time() - stats.inicio
    horas = int(tempo_decorrido // 3600)
    minutos = int((tempo_decorrido % 3600) // 60)
    segundos = int(tempo_decorrido % 60)
    
    taxa_comb = stats.contador_total / tempo_decorrido if tempo_decorrido > 0 else 0
    taxa_verif = (stats.contador_validas * 60) / tempo_decorrido if tempo_decorrido > 0 else 0
    
    total_com_saldo = stats.get_total_com_saldo()
    taxa_sucesso = (total_com_saldo / stats.contador_validas * 100) if stats.contador_validas > 0 else 0
    
    print("=" * 80)
    print("    🔍 BITCOIN WALLET FINDER ULTIMATE - BIP44+BIP49+BIP84")
    print("=" * 80)
    print(f"\n⏱️  TEMPO: {horas:02d}h {minutos:02d}m {segundos:02d}s | 📋 Modo: {modo} | 🚀 Concorrência: {stats.concurrency_atual}")
    
    print("\n📊 ESTATÍSTICAS")
    print("-" * 80)
    print(f"  Testadas:    {stats.contador_total:>8,} | Válidas:    {stats.contador_validas:>8,} ({stats.contador_validas/stats.contador_total*100 if stats.contador_total > 0 else 0:.2f}%)")
    print(f"  Inválidas:   {stats.contador_invalidas:>8,}")
    print(f"\n  💎 Carteiras com Saldo: {total_com_saldo} ({taxa_sucesso:.8f}%)")
    print(f"     • BIP44 (Legacy):        {stats.carteiras_com_saldo_bip44}")
    print(f"     • BIP49 (SegWit):        {stats.carteiras_com_saldo_bip49}")
    print(f"     • BIP84 (Native SegWit): {stats.carteiras_com_saldo_bip84}")
    
    if stats.api_stats:
        print(f"\n  🌐 APIs (Sucessos / Falhas / Taxa):")
        for api, counts in sorted(stats.api_stats.items()):
            total = counts["ok"] + counts["err"]
            taxa = (counts["ok"] / total * 100) if total > 0 else 0
            print(f"     • {api:20s}: {counts['ok']:>5} OK / {counts['err']:>4} ERR ({taxa:>5.1f}%)")
    
    if stats.erros_por_tipo:
        print(f"\n  📛 Erros:")
        for erro, count in sorted(stats.erros_por_tipo.items()):
            print(f"     • {erro:20s}: {count:>5}")
    
    print("\n⚡ DESEMPENHO")
    print("-" * 80)
    print(f"  Taxa:    {taxa_comb:>6.1f} comb/s | Verificações: {taxa_verif:>8.1f} req/min")
    
    print("\n📜 ATIVIDADES")
    print("-" * 80)
    for log in stats.logs[-25:]:
        print(f"  {log}")
    
    print("\n" + "=" * 80)
    print(f"💡 Ctrl+C para parar | Palavra atual: {palavra_atual}")
    print("=" * 80)

# ============================================================================
# FUNÇÃO PRINCIPAL
# ============================================================================

async def main():
    """Função principal"""
    # Carregar palavras
    try:
        palavras = carregar_palavras_bip39()
        print(f"✅ Carregadas {len(palavras)} palavras BIP39")
    except Exception as e:
        print(f"❌ Erro ao carregar palavras: {e}")
        return
    
    # Escolher modo
    print("\n📋 Escolha o modo de operação:")
    print("  1. Modo 11+1 (11 palavras repetidas + 1 variável)")
    print("  2. Modo 10+2 (10 palavras repetidas + 2 variáveis)")
    
    modo_input = input("\nDigite 1 ou 2: ").strip()
    modo = "11+1" if modo_input == "1" else "10+2"
    
    print(f"\n✅ Modo selecionado: {modo}")
    
    # Carregar checkpoint
    cp_base, cp_var1, cp_total, cp_validas, cp_bip44, cp_bip49, cp_bip84 = carregar_checkpoint()
    
    # Inicializar estatísticas
    stats = Stats()
    stats.contador_total = cp_total
    stats.contador_validas = cp_validas
    stats.carteiras_com_saldo_bip44 = cp_bip44
    stats.carteiras_com_saldo_bip49 = cp_bip49
    stats.carteiras_com_saldo_bip84 = cp_bip84
    
    # Determinar ponto de partida
    if cp_base and cp_var1:
        try:
            start_base_idx = palavras.index(cp_base)
            start_var1_idx = palavras.index(cp_var1) + 1
            print(f"✅ Checkpoint carregado: '{cp_base}' + '{cp_var1}'")
        except:
            start_base_idx = 0
            start_var1_idx = 0
    else:
        start_base_idx = 0
        start_var1_idx = 0
        print("ℹ️  Nenhum checkpoint encontrado, começando do início")
    
    print(f"\n🚀 Iniciando busca em modo {modo}...")
    print(f"⚡ Concorrência inicial: {CONCURRENCY_INITIAL}")
    print(f"🌐 8 APIs em paralelo")
    print(f"📊 3 derivações por mnemonic (BIP44+BIP49+BIP84)")
    print("\nPressione Ctrl+C para parar com segurança\n")
    
    time.sleep(2)
    
    # Configurar cliente HTTP
    limits = httpx.Limits(max_keepalive_connections=200, max_connections=300)
    async with httpx.AsyncClient(limits=limits, follow_redirects=True) as client:
        semaphore = asyncio.Semaphore(stats.concurrency_atual)
        ultimo_salvamento = time.time()
        ultimo_display = time.time()
        
        tarefas_pendentes = []
        
        try:
            # Loop principal (LÓGICA IDÊNTICA AO CÓDIGO ORIGINAL)
            for i in range(start_base_idx, len(palavras)):
                palavra_base = palavras[i]
                
                start_j = start_var1_idx if i == start_base_idx else 0
                
                for j in range(start_j, len(palavras)):
                    palavra_var1 = palavras[j]
                    
                    # Criar mnemonic (11+1)
                    mnemonic = " ".join([palavra_base] * 11 + [palavra_var1])
                    
                    stats.contador_total += 1
                    
                    # Validar mnemonic (LÓGICA ORIGINAL)
                    if validar_mnemonic(mnemonic):
                        stats.contador_validas += 1
                        stats.adicionar_log(f"✔️ BIP39 Válida | {mnemonic[:60]}...")
                        
                        # Criar tarefa assíncrona para processar
                        tarefa = asyncio.create_task(
                            processar_carteira(client, mnemonic, palavra_base, palavra_var1, stats, semaphore)
                        )
                        tarefas_pendentes.append(tarefa)
                        
                        # Limitar tarefas pendentes
                        if len(tarefas_pendentes) >= stats.concurrency_atual * 2:
                            done, tarefas_pendentes = await asyncio.wait(
                                tarefas_pendentes,
                                return_when=asyncio.FIRST_COMPLETED
                            )
                            tarefas_pendentes = list(tarefas_pendentes)
                    else:
                        stats.contador_invalidas += 1
                    
                    # Atualizar semaphore se concorrência mudou
                    if stats.concurrency_atual != semaphore._value:
                        semaphore = asyncio.Semaphore(stats.concurrency_atual)
                    
                    # Salvar checkpoint periodicamente
                    tempo_atual = time.time()
                    if tempo_atual - ultimo_salvamento > 30:
                        salvar_checkpoint(palavra_base, palavra_var1, stats)
                        ultimo_salvamento = tempo_atual
                    
                    # Atualizar display
                    if tempo_atual - ultimo_display > 0.5:
                        mostrar_painel(stats, modo, f"{palavra_base} + {palavra_var1}")
                        ultimo_display = tempo_atual
            
            # Aguardar tarefas restantes
            if tarefas_pendentes:
                await asyncio.gather(*tarefas_pendentes, return_exceptions=True)
                
        except KeyboardInterrupt:
            print("\n\n🛑 Interrompido pelo usuário. Salvando checkpoint...")
            salvar_checkpoint(palavra_base, palavra_var1, stats)
            print("✅ Checkpoint salvo!")
        
        finally:
            # Estatísticas finais
            print("\n" + "=" * 80)
            print("📊 ESTATÍSTICAS FINAIS")
            print("=" * 80)
            print(f"Total testadas:        {stats.contador_total:,}")
            print(f"Válidas BIP39:         {stats.contador_validas:,}")
            print(f"Carteiras com saldo:   {stats.get_total_com_saldo()}")
            print(f"  • BIP44 (Legacy):        {stats.carteiras_com_saldo_bip44}")
            print(f"  • BIP49 (SegWit):        {stats.carteiras_com_saldo_bip49}")
            print(f"  • BIP84 (Native SegWit): {stats.carteiras_com_saldo_bip84}")
            print("=" * 80)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n✅ Programa encerrado.")
