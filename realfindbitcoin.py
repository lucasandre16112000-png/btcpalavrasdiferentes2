#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitcoin Wallet Finder - VERSÃO FINAL V2
========================================
- 2 APIs principais (Mempool + Blockstream)
- 1 API de backup (BlockCypher - só usa se alguma principal cair)
- Rate limiter individual por API
- Distribuição round-robin 50/50
- 6 carteiras em paralelo
- 3 endereços por carteira (BIP44+49+84)
- Sleep 0.1s por carteira
- Modo 11+1 e 10+2
- 120 carteiras/minuto
- Se uma API cair, NÃO sobrecarrega a outra
"""

import os
import sys
import time
import json
import asyncio
import httpx
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from bip_utils import (
    Bip39SeedGenerator,
    Bip39MnemonicValidator,
    Bip39WordsNum,
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

TIMEOUT = 10
CONCURRENCY_PER_API = 3  # Não usado mais
CONCURRENCY_TOTAL = 5  # 5 carteiras em paralelo
CHECKPOINT_FILE = "checkpoint.json"
SALDO_FILE = "saldo.txt"
BIP39_FILE = "bip39-words.txt"
MAX_LOG_LINES = 40

# Limites de APIs
BLOCKCYPHER_LIMIT_HOUR = 98  # 98 requisições por hora (margem de segurança)

# ============================================================================
# RATE LIMITER POR API
# ============================================================================

class APIRateLimiter:
    """Rate limiter individual para cada API"""
    
    def __init__(self, nome: str, req_por_segundo: float, limite_hora: Optional[int] = None):
        self.nome = nome
        self.req_por_segundo = req_por_segundo
        self.intervalo = 1.0 / req_por_segundo
        self.ultima_requisicao = 0
        self.lock = asyncio.Lock()
        
        # Limite por hora (para BlockCypher)
        self.limite_hora = limite_hora
        self.requisicoes_hora = []
        self.ativa = True  # API está ativa?
    
    async def aguardar_vez(self) -> bool:
        """
        Aguarda até poder fazer a próxima requisição
        Retorna False se atingiu limite de hora
        """
        if not self.ativa:
            return False
        
        async with self.lock:
            # Verificar limite por hora
            if self.limite_hora:
                agora = time.time()
                # Remover requisições antigas (mais de 1 hora)
                self.requisicoes_hora = [t for t in self.requisicoes_hora if agora - t < 3600]
                
                if len(self.requisicoes_hora) >= self.limite_hora:
                    # Atingiu limite de hora!
                    return False
            
            # Aguardar intervalo entre requisições
            agora = time.time()
            tempo_desde_ultima = agora - self.ultima_requisicao
            
            if tempo_desde_ultima < self.intervalo:
                espera = self.intervalo - tempo_desde_ultima
                await asyncio.sleep(espera)
            
            self.ultima_requisicao = time.time()
            
            # Registrar requisição (para limite de hora)
            if self.limite_hora:
                self.requisicoes_hora.append(self.ultima_requisicao)
            
            return True
    
    def desativar(self):
        """Desativa a API (quando cai)"""
        self.ativa = False
    
    def ativar(self):
        """Reativa a API"""
        self.ativa = True

# ============================================================================
# CLASSE DE ESTATÍSTICAS
# ============================================================================

class Stats:
    def __init__(self):
        self.contador_total = 0
        self.contador_validas = 0
        self.contador_invalidas = 0
        self.carteiras_verificadas = 0
        self.carteiras_com_saldo_bip44 = 0
        self.carteiras_com_saldo_bip49 = 0
        self.carteiras_com_saldo_bip84 = 0
        
        # Estatísticas por API
        self.api_stats = {
            "Mempool": {"ok": 0, "err": 0},
            "Blockstream": {"ok": 0, "err": 0},
            "BlockCypher": {"ok": 0, "err": 0}
        }
        
        # Erros detalhados
        self.erros_detalhados = {}
        self.ultimos_erros = []
        
        # Logs de atividades
        self.logs = []
        
        self.inicio = time.time()
    
    def registrar_sucesso_api(self, api_name: str):
        """Registra sucesso de uma API"""
        if api_name in self.api_stats:
            self.api_stats[api_name]["ok"] += 1
    
    def registrar_erro_api(self, api_name: str, tipo_erro: str):
        """Registra erro de uma API"""
        if api_name in self.api_stats:
            self.api_stats[api_name]["err"] += 1
        
        # Contar por tipo
        if tipo_erro not in self.erros_detalhados:
            self.erros_detalhados[tipo_erro] = 0
        self.erros_detalhados[tipo_erro] += 1
        
        # Adicionar aos últimos erros
        self.ultimos_erros.append({
            "api": api_name,
            "tipo": tipo_erro,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        })
        if len(self.ultimos_erros) > 10:
            self.ultimos_erros.pop(0)
    
    def adicionar_log(self, mensagem: str):
        """Adiciona log de atividade"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {mensagem}")
        if len(self.logs) > MAX_LOG_LINES:
            self.logs.pop(0)
    
    def mostrar_painel(self):
        """Mostra painel de estatísticas"""
        os.system('clear' if os.name == 'posix' else 'cls')
        
        tempo_decorrido = time.time() - self.inicio
        horas = int(tempo_decorrido // 3600)
        minutos = int((tempo_decorrido % 3600) // 60)
        segundos = int(tempo_decorrido % 60)
        
        total_com_saldo = self.carteiras_com_saldo_bip44 + self.carteiras_com_saldo_bip49 + self.carteiras_com_saldo_bip84
        
        taxa = self.carteiras_verificadas / (tempo_decorrido / 60) if tempo_decorrido > 0 else 0
        
        print("=" * 80)
        print("🔍 BITCOIN WALLET FINDER - VERSÃO FINAL V2")
        print("=" * 80)
        print(f"⏱️  Tempo: {horas:02d}:{minutos:02d}:{segundos:02d}")
        print(f"📊 Testadas: {self.contador_total} | Válidas: {self.contador_validas} | Verificadas: {self.carteiras_verificadas}")
        print(f"💎 Com saldo: {total_com_saldo} (BIP44: {self.carteiras_com_saldo_bip44}, BIP49: {self.carteiras_com_saldo_bip49}, BIP84: {self.carteiras_com_saldo_bip84})")
        print(f"⚡ Taxa: {taxa:.1f} carteiras/min")
        print()
        
        # Status das APIs
        print("🌐 STATUS DAS APIs:")
        for api_name, stats in self.api_stats.items():
            total = stats["ok"] + stats["err"]
            taxa_sucesso = (stats["ok"] / total * 100) if total > 0 else 0
            print(f"  {api_name}: ✅ {stats['ok']} | ❌ {stats['err']} | Taxa: {taxa_sucesso:.1f}%")
        print()
        
        # Erros detalhados
        if self.erros_detalhados:
            print("📛 ERROS POR TIPO:")
            for tipo, count in sorted(self.erros_detalhados.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"  {tipo}: {count}")
            print()
        
        # Últimos erros
        if self.ultimos_erros:
            print("🔍 ÚLTIMOS 10 ERROS:")
            for erro in self.ultimos_erros[-10:]:
                print(f"  [{erro['timestamp']}] {erro['api']}: {erro['tipo']}")
            print()
        
        # Logs de atividades
        print(f"📜 ÚLTIMAS {MAX_LOG_LINES} ATIVIDADES:")
        for log in self.logs[-MAX_LOG_LINES:]:
            print(f"  {log}")
        print("=" * 80)

# ============================================================================
# VALIDAÇÃO E DERIVAÇÃO
# ============================================================================

def validar_mnemonic(mnemonic: str) -> bool:
    """Valida mnemonic BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False

def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Gera seed a partir do mnemonic"""
    return Bip39SeedGenerator(mnemonic).Generate(passphrase)

def derivar_enderecos(seed: bytes) -> Dict[str, Dict[str, str]]:
    """Deriva os 3 tipos de endereços (BIP44, BIP49, BIP84)"""
    enderecos = {}
    
    # BIP44 - Legacy (m/44'/0'/0'/0/0)
    try:
        bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
        bip44_acc = bip44_mst_ctx.Purpose().Coin().Account(0)
        bip44_chain = bip44_acc.Change(Bip44Changes.CHAIN_EXT)
        bip44_addr = bip44_chain.AddressIndex(0)
        
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
        bip49_mst_ctx = Bip49.FromSeed(seed, Bip49Coins.BITCOIN)
        bip49_acc = bip49_mst_ctx.Purpose().Coin().Account(0)
        bip49_chain = bip49_acc.Change(Bip44Changes.CHAIN_EXT)
        bip49_addr = bip49_chain.AddressIndex(0)
        
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
        bip84_mst_ctx = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        bip84_acc = bip84_mst_ctx.Purpose().Coin().Account(0)
        bip84_chain = bip84_acc.Change(Bip44Changes.CHAIN_EXT)
        bip84_addr = bip84_chain.AddressIndex(0)
        
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
# VERIFICAÇÃO DE SALDO - 3 APIs
# ============================================================================

async def verificar_saldo_mempool(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str], Optional[str]]:
    """API 1: Mempool.space"""
    try:
        url = f"https://mempool.space/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            return saldo > 0, saldo, "Mempool", None
        elif response.status_code == 429:
            return None, None, None, "429"
        else:
            return None, None, None, f"HTTP_{response.status_code}"
    except asyncio.TimeoutError:
        return None, None, None, "Timeout"
    except httpx.ConnectError:
        return None, None, None, "ConnectionError"
    except Exception as e:
        return None, None, None, f"Error_{type(e).__name__}"

async def verificar_saldo_blockstream(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str], Optional[str]]:
    """API 2: Blockstream"""
    try:
        url = f"https://blockstream.info/api/address/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            return saldo > 0, saldo, "Blockstream", None
        elif response.status_code == 429:
            return None, None, None, "429"
        else:
            return None, None, None, f"HTTP_{response.status_code}"
    except asyncio.TimeoutError:
        return None, None, None, "Timeout"
    except httpx.ConnectError:
        return None, None, None, "ConnectionError"
    except Exception as e:
        return None, None, None, f"Error_{type(e).__name__}"

async def verificar_saldo_blockcypher(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str], Optional[str]]:
    """API 3: BlockCypher (BACKUP)"""
    try:
        url = f"https://api.blockcypher.com/v1/btc/main/addrs/{endereco}/balance"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('final_balance', 0)
            return saldo > 0, saldo, "BlockCypher", None
        elif response.status_code == 429:
            return None, None, None, "429"
        else:
            return None, None, None, f"HTTP_{response.status_code}"
    except asyncio.TimeoutError:
        return None, None, None, "Timeout"
    except httpx.ConnectError:
        return None, None, None, "ConnectionError"
    except Exception as e:
        return None, None, None, f"Error_{type(e).__name__}"

# ============================================================================
# DISTRIBUIDOR DE APIs
# ============================================================================

class DistribuidorAPIs:
    """Distribui requisições entre APIs com rate limiting"""
    
    def __init__(self):
        self.limiters = {
            "Mempool": APIRateLimiter("Mempool", 3.0),
            "Blockstream": APIRateLimiter("Blockstream", 3.0),
            "BlockCypher": APIRateLimiter("BlockCypher", 1.0, limite_hora=BLOCKCYPHER_LIMIT_HOUR)  # 1 req/s, para em 98 req/hora
        }
        
        # APIs principais (usadas primeiro)
        self.apis_principais = ["Mempool", "Blockstream"]
        self.indice_principal = 0
        
        # API de backup
        self.api_backup = "BlockCypher"
    
    def _escolher_api(self) -> str:
        """Escolhe próxima API disponível (round-robin)"""
        # Tentar APIs principais primeiro
        for _ in range(len(self.apis_principais)):
            api = self.apis_principais[self.indice_principal]
            self.indice_principal = (self.indice_principal + 1) % len(self.apis_principais)
            
            if self.limiters[api].ativa:
                return api
        
        # Se nenhuma principal está ativa, usar backup
        if self.limiters[self.api_backup].ativa:
            return self.api_backup
        
        # Nenhuma API disponível!
        return None
    
    async def verificar_endereco(self, client: httpx.AsyncClient, endereco: str, stats: Stats) -> Tuple[bool, int, Optional[str]]:
        """Verifica saldo de um endereço usando API disponível"""
        
        api_name = self._escolher_api()
        
        if not api_name:
            stats.adicionar_log("❌ Nenhuma API disponível!")
            return False, 0, None
        
        # Aguardar rate limit
        pode_fazer = await self.limiters[api_name].aguardar_vez()
        
        if not pode_fazer:
            # Atingiu limite (BlockCypher)
            stats.adicionar_log(f"⚠️  {api_name} atingiu limite de hora!")
            self.limiters[api_name].desativar()
            return False, 0, None
        
        # Fazer requisição
        try:
            if api_name == "Mempool":
                resultado = await verificar_saldo_mempool(client, endereco)
            elif api_name == "Blockstream":
                resultado = await verificar_saldo_blockstream(client, endereco)
            elif api_name == "BlockCypher":
                resultado = await verificar_saldo_blockcypher(client, endereco)
            
            tem_saldo, saldo, api_retornada, erro = resultado
            
            if erro:
                stats.registrar_erro_api(api_name, erro)
                
                # Se der erro 429, desativar API temporariamente
                if erro == "429":
                    stats.adicionar_log(f"⚠️  {api_name} retornou 429 (rate limit)!")
                    self.limiters[api_name].desativar()
                
                return False, 0, None
            
            stats.registrar_sucesso_api(api_name)
            return tem_saldo, saldo, api_name
            
        except Exception as e:
            stats.registrar_erro_api(api_name, f"Exception_{type(e).__name__}")
            return False, 0, None

# ============================================================================
# PROCESSAMENTO DE CARTEIRA
# ============================================================================

async def processar_carteira(
    client: httpx.AsyncClient,
    mnemonic: str,
    palavra_base: str,
    palavra_var1: str,
    palavra_var2: Optional[str],
    stats: Stats,
    distribuidor: DistribuidorAPIs
):
    """Processa uma carteira: deriva 3 endereços e verifica saldo"""
    
    try:
        # Gerar seed
        seed = mnemonic_para_seed(mnemonic)
        
        # Derivar os 3 tipos de endereços
        enderecos = derivar_enderecos(seed)
        
        # Verificar os 3 endereços SEQUENCIALMENTE
        for bip_type, info in enderecos.items():
            endereco = info["endereco"]
            stats.adicionar_log(f"🔍 {bip_type} | {endereco[:24]}...")
            
            # Verificar saldo (distribuidor escolhe a API)
            tem_saldo, saldo, api_name = await distribuidor.verificar_endereco(client, endereco, stats)
            
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
                    f"{bip_type} ({info['tipo']}) | {endereco[:24]}... | {api_name}"
                )
                
                # Salvar em arquivo
                salvar_carteira_com_saldo(palavra_base, palavra_var1, palavra_var2, mnemonic, info, bip_type, saldo, saldo_btc, api_name)
            else:
                if api_name:
                    stats.adicionar_log(f"⭕ Sem saldo | {bip_type} | {endereco[:24]}... | {api_name}")
                else:
                    stats.adicionar_log(f"❌ Erro | {bip_type} | {endereco[:24]}...")
        
        # Incrementar contador UMA VEZ por carteira
        stats.carteiras_verificadas += 1
        
        # Sleep de 0.1s após processar carteira
        await asyncio.sleep(0.1)
        
    except Exception as e:
        stats.adicionar_log(f"❌ Erro ao processar carteira: {type(e).__name__}")

# ============================================================================
# SALVAMENTO
# ============================================================================

def salvar_carteira_com_saldo(
    palavra_base: str,
    palavra_var1: str,
    palavra_var2: Optional[str],
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
        f.write(f"API: {api_name}\n")
        f.write(f"Tipo: {bip_type} ({info['tipo']})\n")
        f.write(f"Derivação: {info['derivacao']}\n\n")
        
        if palavra_var2:
            f.write(f"Palavra Base: {palavra_base} (repetida 10x)\n")
            f.write(f"Palavras Variáveis: {palavra_var1}, {palavra_var2}\n")
        else:
            f.write(f"Palavra Base: {palavra_base} (repetida 11x)\n")
            f.write(f"Palavra Variável: {palavra_var1}\n")
        
        f.write(f"\nMnemonic:\n{mnemonic}\n\n")
        f.write(f"Endereço: {info['endereco']}\n")
        f.write(f"Saldo: {saldo_sat} satoshis ({saldo_btc:.8f} BTC)\n\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública (HEX): {info['pub_hex']}\n")
        f.write("=" * 80 + "\n\n")

def salvar_checkpoint(palavra_base: str, palavra_var1: str, palavra_var2: Optional[str], stats: Stats, modo: str):
    """Salva checkpoint em JSON"""
    checkpoint = {
        "modo": modo,
        "palavra_base": palavra_base,
        "palavra_var1": palavra_var1,
        "palavra_var2": palavra_var2,
        "contador_total": stats.contador_total,
        "contador_validas": stats.contador_validas,
        "contador_invalidas": stats.contador_invalidas,
        "carteiras_verificadas": stats.carteiras_verificadas,
        "carteiras_com_saldo_bip44": stats.carteiras_com_saldo_bip44,
        "carteiras_com_saldo_bip49": stats.carteiras_com_saldo_bip49,
        "carteiras_com_saldo_bip84": stats.carteiras_com_saldo_bip84,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(checkpoint, f, indent=2)

# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Função principal"""
    
    # Carregar palavras BIP39
    if not os.path.exists(BIP39_FILE):
        print(f"❌ Arquivo {BIP39_FILE} não encontrado!")
        return
    
    with open(BIP39_FILE, 'r') as f:
        palavras = [linha.strip() for linha in f if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"⚠️  Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    # Escolher modo
    print("Escolha o modo:")
    print("1. Modo 11+1 (11 palavras repetidas + 1 variável)")
    print("2. Modo 10+2 (10 palavras repetidas + 2 variáveis)")
    
    escolha = input("Digite 1 ou 2: ").strip()
    modo = "11+1" if escolha == "1" else "10+2"
    
    # Inicializar estatísticas
    stats = Stats()
    
    # Carregar checkpoint se existir
    start_base_idx = 0
    start_var1_idx = 0
    start_var2_idx = 0
    
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, 'r') as f:
            checkpoint = json.load(f)
        
        # Restaurar estatísticas
        stats.contador_total = checkpoint.get('contador_total', 0)
        stats.contador_validas = checkpoint.get('contador_validas', 0)
        stats.contador_invalidas = checkpoint.get('contador_invalidas', 0)
        stats.carteiras_verificadas = checkpoint.get('carteiras_verificadas', 0)
        stats.carteiras_com_saldo_bip44 = checkpoint.get('carteiras_com_saldo_bip44', 0)
        stats.carteiras_com_saldo_bip49 = checkpoint.get('carteiras_com_saldo_bip49', 0)
        stats.carteiras_com_saldo_bip84 = checkpoint.get('carteiras_com_saldo_bip84', 0)
        
        # Encontrar índices
        palavra_base = checkpoint['palavra_base']
        palavra_var1 = checkpoint['palavra_var1']
        palavra_var2 = checkpoint.get('palavra_var2')
        
        try:
            start_base_idx = palavras.index(palavra_base)
            start_var1_idx = palavras.index(palavra_var1)
            
            if modo == "10+2" and palavra_var2:
                start_var2_idx = palavras.index(palavra_var2) + 1
                if start_var2_idx >= len(palavras):
                    start_var1_idx += 1
                    start_var2_idx = 0
                    if start_var1_idx >= len(palavras):
                        start_base_idx += 1
                        start_var1_idx = 0
            else:
                start_var1_idx += 1
                if start_var1_idx >= len(palavras):
                    start_base_idx += 1
                    start_var1_idx = 0
                start_var2_idx = 0
        except ValueError:
            start_base_idx = 0
            start_var1_idx = 0
            start_var2_idx = 0
    else:
        start_base_idx = 0
        start_var1_idx = 0
        start_var2_idx = 0
        print("ℹ️  Nenhum checkpoint encontrado, começando do início")
    
    print(f"\n🚀 Iniciando busca em modo {modo}...")
    print(f"⚡ Concorrência: 5 carteiras em paralelo")
    print(f"🌐 2 APIs principais + 1 backup")
    print(f"📊 3 derivações por mnemonic (BIP44+BIP49+BIP84)")
    print(f"🔒 BlockCypher: 1 req/s, para em 98 req/hora")
    print("\nPressione Ctrl+C para parar com segurança\n")
    
    # Criar cliente HTTP e distribuidor
    async with httpx.AsyncClient() as client:
        distribuidor = DistribuidorAPIs()
        tarefas_pendentes = []
        
        try:
            if modo == "11+1":
                # MODO 11+1
                for i in range(start_base_idx, len(palavras)):
                    palavra_base = palavras[i]
                    
                    start_j = start_var1_idx if i == start_base_idx else 0
                    
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        # Criar mnemonic (11+1)
                        mnemonic = " ".join([palavra_base] * 11 + [palavra_var1])
                        
                        stats.contador_total += 1
                        
                        # Validar mnemonic
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            stats.adicionar_log(f"✔️ BIP39 Válida | {mnemonic[:60]}...")
                            
                            # Criar tarefa assíncrona para processar
                            tarefa = asyncio.create_task(
                                processar_carteira(client, mnemonic, palavra_base, palavra_var1, None, stats, distribuidor)
                            )
                            tarefas_pendentes.append(tarefa)
                            
        # Limitar tarefas pendentes (5 carteiras em paralelo)
        if len(tarefas_pendentes) >= 5:
                                done, tarefas_pendentes = await asyncio.wait(
                                    tarefas_pendentes,
                                    return_when=asyncio.FIRST_COMPLETED
                                )
                                tarefas_pendentes = list(tarefas_pendentes)
                        else:
                            stats.contador_invalidas += 1
                        
                        # Mostrar painel a cada 10 combinações
                        if stats.contador_total % 10 == 0:
                            stats.mostrar_painel()
                        
                        # Salvar checkpoint a cada 100 combinações
                        if stats.contador_total % 100 == 0:
                            salvar_checkpoint(palavra_base, palavra_var1, None, stats, modo)
            
            else:  # modo == "10+2"
                # MODO 10+2
                for i in range(start_base_idx, len(palavras)):
                    palavra_base = palavras[i]
                    
                    start_j = start_var1_idx if i == start_base_idx else 0
                    
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        
                        start_k = start_var2_idx if i == start_base_idx and j == start_var1_idx else 0
                        
                        for k in range(start_k, len(palavras)):
                            palavra_var2 = palavras[k]
                            
                            # Criar mnemonic (10+2)
                            mnemonic = " ".join([palavra_base] * 10 + [palavra_var1, palavra_var2])
                            
                            stats.contador_total += 1
                            
                            # Validar mnemonic
                            if validar_mnemonic(mnemonic):
                                stats.contador_validas += 1
                                stats.adicionar_log(f"✔️ BIP39 Válida | {mnemonic[:60]}...")
                                
                                # Criar tarefa assíncrona para processar
                                tarefa = asyncio.create_task(
                                    processar_carteira(client, mnemonic, palavra_base, palavra_var1, palavra_var2, stats, distribuidor)
                                )
                                tarefas_pendentes.append(tarefa)
                                
                        # Limitar tarefas pendentes (5 carteiras em paralelo)
                        if len(tarefas_pendentes) >= 5:
                                    done, tarefas_pendentes = await asyncio.wait(
                                        tarefas_pendentes,
                                        return_when=asyncio.FIRST_COMPLETED
                                    )
                                    tarefas_pendentes = list(tarefas_pendentes)
                            else:
                                stats.contador_invalidas += 1
                            
                            # Mostrar painel a cada 10 combinações
                            if stats.contador_total % 10 == 0:
                                stats.mostrar_painel()
                            
                            # Salvar checkpoint a cada 100 combinações
                            if stats.contador_total % 100 == 0:
                                salvar_checkpoint(palavra_base, palavra_var1, palavra_var2, stats, modo)
            
            # Aguardar tarefas pendentes finalizarem
            if tarefas_pendentes:
                await asyncio.wait(tarefas_pendentes)
        
        except KeyboardInterrupt:
            print("\n\n⚠️  Programa interrompido pelo usuário")
            
            # Aguardar tarefas pendentes finalizarem
            if tarefas_pendentes:
                print("⏳ Aguardando tarefas pendentes finalizarem...")
                await asyncio.wait(tarefas_pendentes)
            
            # Salvar checkpoint final
            if 'palavra_base' in locals():
                salvar_checkpoint(palavra_base, palavra_var1, palavra_var2 if modo == "10+2" else None, stats, modo)
        
        finally:
            stats.mostrar_painel()
            print("\n✅ Programa finalizado!")

if __name__ == "__main__":
    asyncio.run(main())
