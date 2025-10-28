#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitcoin Wallet Finder - Versão Ultimate com Concorrência Adaptativa
Mantém a lógica original mas otimiza a verificação de saldo com async adaptativo
Suporta modos: 11+1 e 10+2
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
from typing import Optional, Dict, List

# ==================== CONFIGURAÇÕES ====================
CONCURRENCY_INITIAL = 8  # Começa com 8 requisições simultâneas
CONCURRENCY_MIN = 3      # Mínimo em caso de muitos erros
CONCURRENCY_MAX = 15     # Máximo permitido
MAX_RETRIES = 2          # Tentativas por endereço
RETRY_DELAY = 1.0        # Delay entre tentativas
CHECKPOINT_INTERVAL = 30 # Salvar checkpoint a cada X segundos
DISPLAY_UPDATE_INTERVAL = 0.5  # Atualizar display a cada 0.5s
LOG_LINES = 20           # Número de linhas de log visíveis

# ==================== ESTATÍSTICAS GLOBAIS ====================
class Stats:
    def __init__(self):
        self.contador_total = 0
        self.contador_validas = 0
        self.carteiras_verificadas = 0
        self.carteiras_com_saldo = 0
        self.erros_por_tipo = defaultdict(int)
        self.inicio = time.time()
        self.ultima_combinacao = ""
        self.ultimo_endereco = ""
        self.concurrency_atual = CONCURRENCY_INITIAL
        self.erros_429_consecutivos = 0
        self.sucessos_consecutivos = 0
        
    def registrar_erro(self, tipo_erro):
        """Registra um erro e ajusta concorrência se necessário"""
        self.erros_por_tipo[tipo_erro] += 1
        
        if tipo_erro == "429":
            self.erros_429_consecutivos += 1
            self.sucessos_consecutivos = 0
            
            # Se tiver muitos 429 consecutivos, reduzir concorrência
            if self.erros_429_consecutivos >= 3 and self.concurrency_atual > CONCURRENCY_MIN:
                self.concurrency_atual = max(CONCURRENCY_MIN, self.concurrency_atual - 2)
                self.erros_429_consecutivos = 0
        else:
            self.sucessos_consecutivos += 1
            
            # Se tiver muitos sucessos, aumentar concorrência gradualmente
            if self.sucessos_consecutivos >= 50 and self.concurrency_atual < CONCURRENCY_MAX:
                self.concurrency_atual = min(CONCURRENCY_MAX, self.concurrency_atual + 1)
                self.sucessos_consecutivos = 0
    
    def total_erros(self):
        return sum(self.erros_por_tipo.values())
    
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
log_buffer = deque(maxlen=LOG_LINES)

# Semáforo dinâmico (será atualizado conforme necessário)
semaphore = None

def atualizar_semaphore():
    """Atualiza o semáforo global com o novo limite de concorrência"""
    global semaphore
    semaphore = asyncio.Semaphore(stats.concurrency_atual)

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
        'erros_por_tipo': dict(stats.erros_por_tipo),
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

# ==================== LOGGING ====================

def adicionar_log(mensagem):
    """Adiciona mensagem ao buffer de log"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_buffer.append(f"[{timestamp}] {mensagem}")

# ==================== VERIFICAÇÃO DE SALDO ASSÍNCRONA ====================

async def verificar_saldo_async(client: httpx.AsyncClient, endereco: str, mnemonic: str) -> tuple:
    """
    Verifica saldo do endereço de forma assíncrona.
    Retorna: (resultado, tipo_erro)
    - resultado: True (tem saldo), False (sem saldo), None (erro)
    - tipo_erro: string com o tipo de erro ou None
    """
    url = f"https://mempool.space/api/address/{endereco}"
    
    # Log de início
    adicionar_log(f"🔍 Verificando {endereco[:20]}...")
    
    for tentativa in range(MAX_RETRIES):
        async with semaphore:
            try:
                response = await client.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    funded_sum = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    tem_saldo = funded_sum > 0
                    
                    # Log de resultado
                    if tem_saldo:
                        adicionar_log(f"✅ SALDO: SIM | {endereco[:20]}... | {mnemonic[:40]}...")
                    else:
                        adicionar_log(f"⭕ Saldo: NÃO | {endereco[:20]}...")
                    
                    stats.sucessos_consecutivos += 1
                    return tem_saldo, None
                
                elif response.status_code == 429:
                    # Rate limit atingido
                    adicionar_log(f"❌ Erro 429 (Rate Limit) | {endereco[:20]}... | Tentativa {tentativa+1}/{MAX_RETRIES}")
                    stats.registrar_erro("429")
                    
                    if tentativa < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (tentativa + 1))
                        continue
                    else:
                        return None, "429"
                
                else:
                    # Outro erro HTTP
                    adicionar_log(f"❌ Erro HTTP {response.status_code} | {endereco[:20]}...")
                    stats.registrar_erro(f"HTTP_{response.status_code}")
                    return None, f"HTTP_{response.status_code}"
                    
            except httpx.TimeoutException:
                adicionar_log(f"❌ Timeout | {endereco[:20]}... | Tentativa {tentativa+1}/{MAX_RETRIES}")
                stats.registrar_erro("Timeout")
                
                if tentativa < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    return None, "Timeout"
                    
            except httpx.ConnectError:
                adicionar_log(f"❌ Erro de Conexão | {endereco[:20]}...")
                stats.registrar_erro("ConnectError")
                return None, "ConnectError"
                    
            except Exception as e:
                adicionar_log(f"❌ Erro Desconhecido: {str(e)[:30]} | {endereco[:20]}...")
                stats.registrar_erro("Unknown")
                return None, "Unknown"
    
    return None, "MaxRetries"

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
    
    # Calcular porcentagens
    pct_validas = (stats.contador_validas / stats.contador_total * 100) if stats.contador_total > 0 else 0
    pct_sucesso = (stats.carteiras_com_saldo / stats.carteiras_verificadas * 100) if stats.carteiras_verificadas > 0 else 0
    
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER - PAINEL DE MONITORAMENTO ULTIMATE".center(80))
    print("=" * 80)
    print()
    print(f"⏱️  TEMPO: {horas:02d}h {minutos:02d}m {segundos:02d}s | 🚀 Concorrência: {stats.concurrency_atual} req/s")
    print()
    print("📊 ESTATÍSTICAS")
    print("-" * 80)
    print(f"  Testadas: {stats.contador_total:>10,} | Válidas: {stats.contador_validas:>8,} ({pct_validas:.2f}%)")
    print(f"  Verificadas: {stats.carteiras_verificadas:>7,} | Com Saldo: {stats.carteiras_com_saldo:>5,} ({pct_sucesso:.8f}%)")
    print(f"  Erros Total: {stats.total_erros():>7,}")
    
    # Detalhamento de erros
    if stats.erros_por_tipo:
        print(f"\n  📛 Erros por Tipo:")
        for tipo, count in sorted(stats.erros_por_tipo.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"     • {tipo}: {count}")
    
    print()
    print("⚡ DESEMPENHO")
    print("-" * 80)
    print(f"  Taxa Combinações: {taxa_comb:>8.1f} comb/s | Verificações: {taxa_verif:>8.1f} req/min")
    print()
    print("📜 ÚLTIMAS ATIVIDADES (20 linhas)")
    print("-" * 80)
    
    # Exibir log buffer
    if log_buffer:
        for linha in log_buffer:
            print(f"  {linha}")
    else:
        print("  Aguardando atividade...")
    
    print()
    print("=" * 80)
    print("💡 Ctrl+C para parar | Checkpoint automático a cada 30s")
    print("=" * 80)

# ==================== FUNÇÃO PRINCIPAL ====================

async def main_async():
    """Função principal assíncrona"""
    global semaphore
    
    # Inicializar semáforo
    atualizar_semaphore()
    
    # Carregar palavras BIP39
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    
    # Escolher modo
    limpar_tela()
    print("=" * 80)
    print("🔍 BITCOIN WALLET FINDER ULTIMATE".center(80))
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
        
        # Carregar erros por tipo
        erros_salvos = checkpoint.get('erros_por_tipo', {})
        for tipo, count in erros_salvos.items():
            stats.erros_por_tipo[tipo] = count
        
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
                    # Modo 11+1
                    for j in range(start_j, len(palavras)):
                        palavra_var1 = palavras[j]
                        stats.contador_total += 1
                        
                        # Criar mnemonic
                        mnemonic = criar_mnemonic(palavra_base, palavra_var1, None, modo)
                        stats.ultima_combinacao = mnemonic
                        
                        # Validar mnemonic (RÁPIDO, LOCAL)
                        if validar_mnemonic(mnemonic):
                            stats.contador_validas += 1
                            adicionar_log(f"✔️ Válida BIP39 | {mnemonic[:50]}...")
                            
                            # Gerar carteira (RÁPIDO, LOCAL)
                            seed = mnemonic_para_seed(mnemonic)
                            addr_index = derivar_bip44_btc(seed)
                            info = mostrar_info(addr_index)
                            
                            # Atualizar semáforo se necessário
                            if stats.concurrency_atual != semaphore._value:
                                atualizar_semaphore()
                            
                            # Verificar saldo (LENTO, ONLINE) - ASSÍNCRONO
                            resultado, erro = await verificar_saldo_async(client, info['address'], mnemonic)
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
                    # Modo 10+2
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
                                adicionar_log(f"✔️ Válida BIP39 | {mnemonic[:50]}...")
                                
                                # Gerar carteira (RÁPIDO, LOCAL)
                                seed = mnemonic_para_seed(mnemonic)
                                addr_index = derivar_bip44_btc(seed)
                                info = mostrar_info(addr_index)
                                
                                # Atualizar semáforo se necessário
                                if stats.concurrency_atual != semaphore._value:
                                    atualizar_semaphore()
                                
                                # Verificar saldo (LENTO, ONLINE) - ASSÍNCRONO
                                resultado, erro = await verificar_saldo_async(client, info['address'], mnemonic)
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
            adicionar_log("⚠️ Programa interrompido pelo usuário")
        
        finally:
            # Salvar checkpoint final
            exibir_painel()
            print("\n✓ Checkpoint final salvo!")
            print(f"\n📁 Arquivos gerados:")
            print(f"  • checkpoint.json - Checkpoint para retomar")
            if stats.carteiras_com_saldo > 0:
                print(f"  • saldo.txt - {stats.carteiras_com_saldo} carteira(s) com saldo")

def main():
    """Ponto de entrada"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n\n👋 Programa encerrado")

if __name__ == "__main__":
    main()
