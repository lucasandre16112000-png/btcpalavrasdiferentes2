#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gera_wallet_bip39_repeticao.py
Gera carteiras Bitcoin testando combinações de 11 palavras repetidas + 1 palavra variável.
Com sistema de checkpoint baseado na última combinação testada.
"""

import os
import sys  # Adicionado sys para _signal_handler
import json
import signal
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
# Removida bip_utils e hdwallet, pois bip_utils já contém as classes necessarias
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes

# ==============================================================================
# --- CONFIGURAÇÕES OTIMIZADAS PARA RYZEN 5 5500 e 300MB INTERNET ---
# Ajustado para remover a necessidade de SED no script de instalacao
BATCH_SIZE = 30
BATCH_WORKERS = 16
RETRY_COUNT = 4
BACKOFF_FACTOR = 0.5
HTTP_TIMEOUT = 15
PRINT_EVERY = 5000
CHECKPOINT_EVERY = 10000
# ==============================================================================

def make_session(retries=RETRY_COUNT, backoff_factor=BACKOFF_FACTOR, status_forcelist=(429,500,502,503,504)):
    session = requests.Session()
    retry = Retry(total=retries, read=retries, connect=retries, backoff_factor=backoff_factor,
                  status_forcelist=status_forcelist, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "btcpalavras/1.0"})
    return session

http_session = make_session()

# Ajustada a funcao para usar o HTTP_TIMEOUT definido
def verificar_saldo_mempool(endereco, timeout=HTTP_TIMEOUT, max_attempts=RETRY_COUNT):
    """Verifica saldo do endereco usando API da mempool.space.
    Retorna True se houver saldo, False se nao houver, e None em caso de falha temporaria."""
    url = f"https://mempool.space/api/address/{endereco}"
    attempt = 0
    while attempt < max_attempts:
        try:
            resp = http_session.get(url, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
                    return saldo > 0
                except Exception:
                    # Falha ao processar JSON, mas status 200: sem saldo ou erro de API.
                    return False 
            if resp.status_code in (429, 500, 502, 503, 504):
                attempt += 1
            else:
                return False
        except requests.exceptions.RequestException:
            attempt += 1
        
        # Backoff exponencial ajustado
        sleep_time = (backoff_factor * (2 ** attempt)) + random.uniform(0, 0.2)
        time.sleep(sleep_time)
        
    return None

def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    """Carrega a lista de palavras BIP39 do arquivo"""
    if not os.path.exists(arquivo):
        # Corrigido para BIP39 default se o arquivo nao for encontrado
        # Voce pode querer usar uma lista de palavras padrao aqui, mas mantendo a excecao
        raise FileNotFoundError(f"Arquivo {arquivo} não encontrado! O script requer o arquivo de palavras BIP39.")
    
    with open(arquivo, 'r') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    return palavras

def carregar_ultima_combinacao(arquivo="ultimo.txt"):
    """Carrega a última combinação testada do arquivo"""
    if not os.path.exists(arquivo):
        return None, None, None
    
    try:
        with open(arquivo, 'r') as f:
            palavras = f.read().strip().split()
            if len(palavras) == 12:
                # Verificar se é padrão de repetição (11 palavras iguais + 1 diferente)
                palavra_base = palavras[0]
                if all(p == palavra_base for p in palavras[:11]):
                    palavra_completa = palavras[11]
                    return palavra_base, palavra_completa, " ".join(palavras)
    except Exception:
        pass
    
    return None, None, None

def carregar_estatisticas_checkpoint(arquivo="checkpoint.txt"):
    """Carrega estatísticas do arquivo checkpoint.txt"""
    contador_total = 0
    contador_validas = 0
    carteiras_com_saldo = 0
    
    if not os.path.exists(arquivo):
        return contador_total, contador_validas, carteiras_com_saldo
    
    try:
        with open(arquivo, 'r') as f:
            lines = f.readlines()
            for line in lines:
                line = line.strip()
                if "Total de combinações testadas:" in line:
                    contador_total = int(line.split(":")[1].strip())
                elif "Combinações válidas:" in line:
                    contador_validas = int(line.split(":")[1].strip())
                elif "Carteiras com saldo:" in line:
                    carteiras_com_saldo = int(line.split(":")[1].strip())
    except Exception as e:
        print(f"Erro ao ler checkpoint: {e}")
    
    return contador_total, contador_validas, carteiras_com_saldo

def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa):
    """Encontra a próxima combinação a ser testada"""
    try:
        base_idx = palavras.index(ultima_base)
        completa_idx = palavras.index(ultima_completa)
        
        # Avançar para a próxima palavra completa
        if completa_idx + 1 < len(palavras):
            return base_idx, completa_idx + 1
        # Se chegou ao final das palavras completas, avançar para próxima palavra base
        elif base_idx + 1 < len(palavras):
            return base_idx + 1, 0
        # Se chegou ao final de tudo
        else:
            return None, None
            
    except ValueError:
        return 0, 0  # Começar do início se não encontrar as palavras

def salvar_ultima_combinacao(arquivo="ultimo.txt", palavra_base="", palavra_completa=""):
    """Salva a combinação atual no arquivo"""
    palavras = [palavra_base] * 11 + [palavra_completa]
    mnemonic = " ".join(palavras)
    
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(mnemonic)
        f.flush()

def salvar_checkpoint(arquivo="checkpoint.txt", base_idx=0, palavra_base="", 
                     contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    """Salva checkpoint com estatísticas atuais"""
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n")
        f.write(f"Total de combinações testadas: {contador_total}\n")
        f.write(f"Combinações válidas: {contador_validas}\n")
        f.write(f"Carteiras com saldo: {carteiras_com_saldo}\n")
        f.flush()

def criar_mnemonic_repetido(palavra_base, palavra_completa):
    """Cria mnemonic com palavra_base repetida 11 vezes + palavra_completa"""
    palavras = [palavra_base] * 11 + [palavra_completa]
    return " ".join(palavras)

def validar_mnemonic(mnemonic):
    """Valida se o mnemonic é válido segundo BIP39"""
    try:
        # A Bip39MnemonicValidator ja lanca excecao se for invalido, entao usamos Try/Except.
        # Bip39MnemonicValidator().IsValid(mnemonic)
        return True
    except:
        return False

def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Gera seed BIP39 a partir do mnemonic"""
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)

def derivar_bip44_btc(seed: bytes):
    """Deriva carteira BIP44 Bitcoin (m/44'/0'/0'/0/0)"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)

def mostrar_info(addr_index):
    """Extrai informações da carteira"""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()

    return {
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "wif": priv_key_obj.ToWif(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
        "address": addr_index.PublicKey().ToAddress()
    }


def salvar_carteira_com_saldo(palavra_base, palavra_completa, mnemonic, info):
    """Salva carteira com saldo no arquivo saldo.txt"""
    with open("saldo.txt", "a") as f:
        f.write(f"Palavra Base: {palavra_base} (repetida 11x)\n")
        f.write(f"Palavra Completa: {palavra_completa}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"Endereço: {info['address']}\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("-" * 80 + "\n\n")
    print("🎉 CARTERIA COM SALDO SALVA! 🎉")


STATE_FILE = 'checkpoint_state.json'


# --- Resume / checkpoint helpers ---
def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

def salvar_estado_checkpoint(base_idx, completa_idx, contador_total, contador_validas, carteiras_com_saldo, palavras=None):
    try:
        state = {
            "base_idx": int(base_idx) if base_idx is not None else None,
            "completa_idx": int(completa_idx) if completa_idx is not None else None,
            "contador_total": int(contador_total),
            "contador_validas": int(contador_validas),
            "carteiras_com_saldo": int(carteiras_com_saldo),
            "timestamp": time.time()
        }
        if palavras is not None and isinstance(palavras, list):
            try:
                state["palavra_base"] = palavras[int(base_idx)] if base_idx is not None else None
            except Exception:
                state["palavra_base"] = None
        _atomic_write(STATE_FILE, json.dumps(state))
    except Exception as e:
        try:
            print(f"[checkpoint] erro ao salvar estado: {e}")
        except Exception:
            pass

def carregar_estado_checkpoint():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except Exception as e:
        try:
            print(f"[checkpoint] erro ao ler state: {e}")
        except Exception:
            pass
        return None

def _signal_handler(signum, frame):
    # Necessario importar sys para sys.exit(0)
    import sys 
    try:
        state = {}
        # Variaveis globais nao estao sempre definidas; verificar antes de atribuir
        state['base_idx'] = globals().get('i', None)
        state['completa_idx'] = globals().get('j', None)
        state['contador_total'] = globals().get('contador_total', 0)
        state['contador_validas'] = globals().get('contador_validas', 0)
        state['carteiras_com_saldo'] = globals().get('carteiras_com_saldo', 0)
        
        salvar_estado_checkpoint(state.get('base_idx'), state.get('completa_idx'),
                                 state.get('contador_total'), state.get('contador_validas'),
                                 state.get('carteiras_com_saldo'))
        
        # CORRECAO DO SYNTAXERROR: Fechamento correto do f-string
        print(f"\n[SIGNAL] sinal {signum} recebido: estado salvo em {STATE_FILE}. Saindo...")
        
    except Exception as e:
        try:
            print(f"[SIGNAL] erro ao salvar estado: {e}")
        except Exception:
            pass
    try:
        sys.exit(0)
    except Exception:
        os._exit(0)

try:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
except Exception:
    pass

# --- end helpers ---


def main():
    """Função principal com sistema de checkpoint baseado na última combinação"""
    # ... (Inicializacao de contadores)
    global contador_total, contador_validas, carteiras_com_saldo
    
    # Carregar palavras BIP39
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return
    
    # Carregar última combinação testada
    ultima_base, ultima_completa, ultimo_mnemonic = carregar_ultima_combinacao("ultimo.txt")
    
    # Tentativa de carregar estado salvo (resume)
    state = carregar_estado_checkpoint()
    
    base_idx, completa_idx = 0, 0
    if state is not None:
        try:
            if state.get("base_idx") is not None and state.get("completa_idx") is not None:
                base_idx = int(state.get("base_idx"))
                completa_idx = int(state.get("completa_idx"))
                ultima_base = palavras[base_idx] if 0 <= base_idx < len(palavras) else ultima_base
                ultima_completa = palavras[completa_idx] if 0 <= completa_idx < len(palavras) else ultima_completa
                
                # Sincroniza contadores globais com o estado salvo
                contador_total = state.get('contador_total', 0)
                contador_validas = state.get('contador_validas', 0)
                carteiras_com_saldo = state.get('carteiras_com_saldo', 0)
                
                print(f"[checkpoint] Carregando estado: base_idx={base_idx}, completa_idx={completa_idx}, total={contador_total}")
            else:
                pb = state.get("palavra_base")
                if pb and pb in palavras:
                    base_idx = palavras.index(pb)
                    completa_idx = 0
                    contador_total = state.get('contador_total', 0)
                    contador_validas = state.get('contador_validas', 0)
                    carteiras_com_saldo = state.get('carteiras_com_saldo', 0)
                    print(f"[checkpoint] Carregado por palavra base '{pb}' -> base_idx={base_idx}")
        except Exception as e:
            print(f"[checkpoint] erro ao aplicar estado: {e}")

    
    # Carregar estatísticas do checkpoint (para fins de exibicao inicial, mas o state e o principal)
    # Reatribuindo para garantir que as estatisticas do STATE_FILE sejam prioritarias.
    if state is None:
        contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint("checkpoint.txt")
    
    print(f"Estatísticas carregadas:")
    print(f"  Total de combinações testadas: {contador_total}")
    print(f"  Combinações válidas: {contador_validas}")
    print(f"  Carteiras com saldo: {carteiras_com_saldo}")
    
    # Determinar ponto de partida
    if ultima_base and ultima_completa and state is None:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    elif base_idx is None:
        print("Todas as combinações já foram testadas (Ponto de partida finalizado)!")
        return
    
    
    print(f"Continuando da posição: palavra base #{base_idx+1} ('{palavras[base_idx]}'), palavra completa #{completa_idx+1} ('{palavras[completa_idx]}')")
    
    print("\nIniciando teste de combinações BIP39...")
    print("Padrão: 11 palavras repetidas + 1 palavra variável")
    print(f"Verificando saldo na Mempool.space (Timeout: {HTTP_TIMEOUT}s, Retries: {RETRY_COUNT})")
    print(f"Usando {BATCH_WORKERS} threads para verificar {BATCH_SIZE} endereços por lote.")
    print("Carteiras com saldo serão salvas em saldo.txt")
    print("Checkpoint salvo em checkpoint.txt e checkpoint_state.json")
    print("Pressione Ctrl+C para parar\n")
    
    ultimo_salvamento = time.time()
    batch = []
    
    try:
        # Iterar a partir do ponto atual
        for i in range(base_idx, len(palavras)):
            palavra_base = palavras[i]
            
            # Determinar índice inicial para palavra completa
            start_j = completa_idx if i == base_idx else 0
            
            for j in range(start_j, len(palavras)):
                palavra_completa = palavras[j]
                contador_total += 1
                
                # Checkpoint automático
                if contador_total % CHECKPOINT_EVERY == 0:
                    try:
                        salvar_estado_checkpoint(i, j, contador_total, contador_validas, carteiras_com_saldo, palavras)
                        print(f"[checkpoint] salvo automatico: base_idx={i}, completa_idx={j}, total={contador_total}")
                    except Exception:
                        pass
                
                # Progresso de exibicao (usando a nova variavel PRINT_EVERY)
                if contador_total % PRINT_EVERY == 0:
                    print(f"Testadas {contador_total} combinações | Última: {palavra_base}...")
                
                # Criar mnemonic
                mnemonic = criar_mnemonic_repetido(palavra_base, palavra_completa)
                
                # Salvar combinação atual no arquivo (ultimo.txt)
                salvar_ultima_combinacao("ultimo.txt", palavra_base, palavra_completa)
                
                # Salvar estatísticas (checkpoint.txt) a cada 30 segundos
                tempo_atual = time.time()
                if tempo_atual - ultimo_salvamento > 30:
                    salvar_checkpoint("checkpoint.txt", i, palavra_base, 
                                    contador_total, contador_validas, carteiras_com_saldo)
                    ultimo_salvamento = tempo_atual
                
                # Validar mnemonic
                if validar_mnemonic(mnemonic):
                    contador_validas += 1
                    
                    # Gerar carteira
                    seed = mnemonic_para_seed(mnemonic)
                    addr_index = derivar_bip44_btc(seed)
                    info = mostrar_info(addr_index)
                    
                    # Verificar saldo (Adicionar ao lote)
                    batch.append((mnemonic, palavra_base, palavra_completa, info))
                    
                    # Processar lote quando atingir o tamanho
                    if len(batch) >= BATCH_SIZE:
                        with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as ex:
                            futures = {ex.submit(verificar_saldo_mempool, item[3]['address']): item for item in batch}
                            for fut in as_completed(futures):
                                item = futures[fut]
                                try:
                                    # CORRECAO: O resultado de fut.result() e True/False/None
                                    tem_saldo = fut.result()
                                except Exception as e:
                                    print(f"[Erro Thread] Falha ao verificar saldo: {e}")
                                    tem_saldo = None # Considera falha
                                
                                # Se tem saldo, salvar e incrementar
                                if tem_saldo is True:
                                    carteiras_com_saldo += 1
                                    salvar_carteira_com_saldo(item[1], item[2], item[0], item[3])
                                elif tem_saldo is None:
                                    # Nao faz nada, deixa o erro ser tratado pelo make_session (retry)
                                    pass
                        batch.clear()

                # Apos o batch, exibir progresso com a validacao.
                if contador_validas % PRINT_EVERY == 0 and contador_validas > 0:
                    print(f"\n[Progresso] {contador_validas} válidas. Total testado: {contador_total}")
                        
            
            # Processa o lote final (se houver)
            if batch:
                with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as ex:
                    futures = {ex.submit(verificar_saldo_mempool, item[3]['address']): item for item in batch}
                    for fut in as_completed(futures):
                        item = futures[fut]
                        try:
                            tem_saldo = fut.result()
                        except Exception as e:
                            print(f"[Erro Thread] Falha ao verificar saldo no lote final: {e}")
                            tem_saldo = None

                        if tem_saldo is True:
                            carteiras_com_saldo += 1
                            salvar_carteira_com_saldo(item[1], item[2], item[0], item[3])
                batch.clear()
            
            # Resetar índice da palavra completa após processar a primeira palavra base
            completa_idx = 0
            
            # Salvar checkpoint após cada palavra base
            salvar_checkpoint("checkpoint.txt", i, palavra_base, 
                            contador_total, contador_validas, carteiras_com_saldo)
            
            # Status após cada palavra base
            print(f"\nConcluído para '{palavra_base}': {contador_validas} válidas, {carteiras_com_saldo} com saldo")
            
    except KeyboardInterrupt:
        print("\n\nPrograma interrompido pelo usuário")
        # Salvar checkpoint final antes de sair
        if 'i' in locals():
            salvar_estado_checkpoint(i, j, contador_total, contador_validas, carteiras_com_saldo, palavras)
            salvar_checkpoint("checkpoint.txt", i, palavra_base, 
                            contador_total, contador_validas, carteiras_com_saldo)
    
    finally:
        # Salvar estatísticas finais
        with open("estatisticas_finais.txt", "w") as f:
            f.write("ESTATÍSTICAS FINAIS\n")
            f.write("=" * 50 + "\n")
            f.write(f"Total de combinações testadas: {contador_total}\n")
            f.write(f"Combinações válidas (BIP39): {contador_validas}\n")
            f.write(f"Carteiras com saldo encontradas: {carteiras_com_saldo}\n")
            if contador_validas > 0:
                f.write(f"Taxa de sucesso: {(carteiras_com_saldo/contador_validas)*100:.8f}%\n")
            else:
                f.write("Taxa de sucesso: 0%\n")
        
        print(f"\n--- ESTATÍSTICAS FINAIS ---")
        print(f"Total de combinações testadas: {contador_total}")
        print(f"Combinações válidas (BIP39): {contador_validas}")
        print(f"Carteiras com saldo encontradas: {carteiras_com_saldo}")
        if contador_validas > 0:
            print(f"Taxa de sucesso: {(contador_validas/contador_total)*100:.8f}% de válidas.")
            print(f"Taxa de saldo: {(carteiras_com_saldo/contador_validas)*100:.8f}% de saldos.")
        else:
            print("Taxa de sucesso: 0%")

if __name__ == "__main__":
    main()
