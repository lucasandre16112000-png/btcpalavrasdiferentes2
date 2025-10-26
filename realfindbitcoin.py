#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realfindbitcoin.py
Gera carteiras Bitcoin testando combinações de 10 palavras repetidas + 2 palavras variáveis.
Com sistema de checkpoint e recuperação da última combinação testada.
"""

import os
import time
import requests
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes


# =============================================================
# 🔹 Funções auxiliares de carregamento e checkpoint
# =============================================================

def carregar_palavras_bip39(arquivo="bip39-words.txt"):
    """Carrega a lista de palavras BIP39 do arquivo"""
    if not os.path.exists(arquivo):
        raise FileNotFoundError(f"Arquivo {arquivo} não encontrado!")
    
    with open(arquivo, 'r', encoding='utf-8') as f:
        palavras = [linha.strip() for linha in f.readlines() if linha.strip()]
    
    if len(palavras) != 2048:
        print(f"Aviso: Esperadas 2048 palavras, encontradas {len(palavras)}")
    
    return palavras


def carregar_ultima_combinacao(arquivo="ultimo.txt"):
    """Carrega a última combinação testada (10 repetidas + 2 variáveis)"""
    if not os.path.exists(arquivo):
        return None, None, None, None
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            palavras = f.read().strip().split()
            if len(palavras) == 12:
                palavra_base = palavras[0]
                if all(p == palavra_base for p in palavras[:10]):
                    return palavra_base, palavras[10], palavras[11], " ".join(palavras)
    except Exception:
        pass
    return None, None, None, None


def carregar_estatisticas_checkpoint(arquivo="checkpoint.txt"):
    """Carrega estatísticas salvas em checkpoint.txt"""
    contador_total = contador_validas = carteiras_com_saldo = 0
    if not os.path.exists(arquivo):
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


def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2):
    """Encontra a próxima combinação após a última testada"""
    try:
        base_idx = palavras.index(ultima_base)
        completa_idx = palavras.index(ultima_completa2)
        if completa_idx + 1 < len(palavras):
            return base_idx, completa_idx + 1
        elif base_idx + 1 < len(palavras):
            return base_idx + 1, 0
        else:
            return None, None
    except ValueError:
        return 0, 0


def salvar_ultima_combinacao(arquivo="ultimo.txt", palavra_base="", palavra_completa1="", palavra_completa2=""):
    """Salva a combinação atual (10+2)"""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    mnemonic = " ".join(palavras)
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(mnemonic)


def salvar_checkpoint(arquivo="checkpoint.txt", base_idx=0, palavra_base="",
                      contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    """Salva estatísticas e progresso"""
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n")
        f.write(f"Total de combinações testadas: {contador_total}\n")
        f.write(f"Combinações válidas: {contador_validas}\n")
        f.write(f"Carteiras com saldo: {carteiras_com_saldo}\n")


# =============================================================
# 🔹 Funções principais de geração e verificação
# =============================================================

def criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2):
    """Cria mnemonic com 10 repetidas + 2 variáveis"""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    return " ".join(palavras)


def validar_mnemonic(mnemonic):
    """Valida se o mnemonic é válido segundo BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
    except:
        return False


def mnemonic_para_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """Gera seed BIP39"""
    seed_gen = Bip39SeedGenerator(mnemonic)
    return seed_gen.Generate(passphrase)


def derivar_bip44_btc(seed: bytes):
    """Deriva carteira padrão BIP44 Bitcoin"""
    bip44_mst_ctx = Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
    acct = bip44_mst_ctx.Purpose().Coin().Account(0)
    change = acct.Change(Bip44Changes.CHAIN_EXT)
    return change.AddressIndex(0)


def mostrar_info(addr_index):
    """Extrai chaves e endereço"""
    priv_key_obj = addr_index.PrivateKey()
    pub_key_obj = addr_index.PublicKey()
    return {
        "priv_hex": priv_key_obj.Raw().ToHex(),
        "wif": priv_key_obj.ToWif(),
        "pub_compressed_hex": pub_key_obj.RawCompressed().ToHex(),
        "address": addr_index.PublicKey().ToAddress()
    }


def verificar_saldo_mempool(endereco):
    """Verifica saldo usando API da Mempool.space"""
    try:
        url = f"https://mempool.space/api/address/{endereco}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            saldo = data.get('chain_stats', {}).get('funded_txo_sum', 0)
            return saldo > 0
        return False
    except Exception as e:
        print(f"Erro ao verificar saldo: {e}")
        return False


def salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info):
    """Salva no arquivo saldo.txt"""
    with open("saldo.txt", "a", encoding='utf-8') as f:
        f.write(f"Palavra Base: {palavra_base} (repetida 10x)\n")
        f.write(f"Palavras Finais: {palavra_completa1}, {palavra_completa2}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"Endereço: {info['address']}\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("-" * 80 + "\n\n")
    print("🎉 Carteira com saldo salva!")


# =============================================================
# 🔹 Função principal
# =============================================================

def main():
    """Função principal com sistema de checkpoint"""
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return

    ultima_base, ultima_completa1, ultima_completa2, ultimo_mnemonic = carregar_ultima_combinacao("ultimo.txt")
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint("checkpoint.txt")

    print(f"\nEstatísticas carregadas:")
    print(f"  Total testadas: {contador_total}")
    print(f"  Válidas: {contador_validas}")
    print(f"  Com saldo: {carteiras_com_saldo}\n")

    if ultima_base and ultima_completa1 and ultima_completa2:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        print("Nenhum checkpoint encontrado, começando do início...\n")
        base_idx, completa_idx = 0, 0

    print(f"Continuando de '{palavras[base_idx]}' (base), iniciando variação #{completa_idx+1}")
    print("\nIniciando geração de combinações 10+2 BIP39...\n")

    ultimo_salvamento = time.time()

    try:
        for i in range(base_idx, len(palavras)):
            palavra_base = palavras[i]
            start_j = completa_idx if i == base_idx else 0

            for j in range(start_j, len(palavras) - 1):
                palavra_completa1 = palavras[j]
                palavra_completa2 = palavras[j + 1]
                contador_total += 1

                mnemonic = criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2)
                salvar_ultima_combinacao("ultimo.txt", palavra_base, palavra_completa1, palavra_completa2)

                tempo_atual = time.time()
                if tempo_atual - ultimo_salvamento > 30 or contador_total % 100 == 0:
                    salvar_checkpoint("checkpoint.txt", i, palavra_base, contador_total, contador_validas, carteiras_com_saldo)
                    ultimo_salvamento = tempo_atual

                if contador_total % 100 == 0:
                    print(f"Testadas {contador_total} combinações | Última: {mnemonic}")

                if validar_mnemonic(mnemonic):
                    contador_validas += 1
                    seed = mnemonic_para_seed(mnemonic)
                    addr_index = derivar_bip44_btc(seed)
                    info = mostrar_info(addr_index)
                    if verificar_saldo_mempool(info["address"]):
                        carteiras_com_saldo += 1
                        salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info)
                    time.sleep(0.1)

            completa_idx = 0
            salvar_checkpoint("checkpoint.txt", i, palavra_base, contador_total, contador_validas, carteiras_com_saldo)
            print(f"\nConcluído para '{palavra_base}': {contador_validas} válidas, {carteiras_com_saldo} com saldo\n")

    except KeyboardInterrupt:
        print("\n🟡 Execução interrompida manualmente.")
        salvar_checkpoint("checkpoint.txt", i, palavra_base, contador_total, contador_validas, carteiras_com_saldo)

    finally:
        with open("estatisticas_finais.txt", "w", encoding='utf-8') as f:
            f.write("ESTATÍSTICAS FINAIS\n" + "=" * 50 + "\n")
            f.write(f"Total testadas: {contador_total}\n")
            f.write(f"Válidas: {contador_validas}\n")
            f.write(f"Com saldo: {carteiras_com_saldo}\n")
        print("\n✅ Execução finalizada com sucesso!")


if __name__ == "__main__":
    main()
