#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gera_wallet_bip39_repeticao.py (arquivo local corrigido)
Gera carteiras Bitcoin testando combinações de 10 palavras repetidas + 2 palavras variáveis.
Com sistema de checkpoint baseado na última combinação testada.
"""

import os
import time
import requests
from bip_utils import Bip39SeedGenerator, Bip39MnemonicValidator
from bip_utils import Bip44, Bip44Coins, Bip44Changes


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
    """Carrega a última combinação testada do arquivo.
    Retorna (palavra_base, palavra_completa1, palavra_completa2, mnemonic) ou (None, None, None, None)
    Espera formato de 12 palavras: 10 repetidas + 2 variáveis.
    """
    if not os.path.exists(arquivo):
        return None, None, None, None
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            palavras = f.read().strip().split()
            if len(palavras) == 12:
                palavra_base = palavras[0]
                # verificar padrão 10 repetidas
                if all(p == palavra_base for p in palavras[:10]):
                    return palavra_base, palavras[10], palavras[11], " ".join(palavras)
    except Exception:
        pass
    return None, None, None, None


def carregar_estatisticas_checkpoint(arquivo="checkpoint.txt"):
    """Carrega estatísticas do arquivo checkpoint.txt"""
    contador_total = 0
    contador_validas = 0
    carteiras_com_saldo = 0
    if not os.path.exists(arquivo):
        return contador_total, contador_validas, carteiras_com_saldo
    try:
        with open(arquivo, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                line = line.strip()
                if "Total de combinações testadas:" in line:
                    try:
                        contador_total = int(line.split(":")[1].strip())
                    except:
                        pass
                elif "Combinações válidas:" in line:
                    try:
                        contador_validas = int(line.split(":")[1].strip())
                    except:
                        pass
                elif "Carteiras com saldo:" in line:
                    try:
                        carteiras_com_saldo = int(line.split(":")[1].strip())
                    except:
                        pass
    except Exception as e:
        print(f"Erro ao ler checkpoint: {e}")
    return contador_total, contador_validas, carteiras_com_saldo


def encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2):
    """Encontra a próxima combinação a ser testada a partir de (base, completa2)"""
    try:
        base_idx = palavras.index(ultima_base)
        completa_idx = palavras.index(ultima_completa2)
        # próxima dupla: avançar completa2 (o código gera pares j, j+1)
        if completa_idx + 1 < len(palavras) - 0:
            return base_idx, completa_idx + 1
        elif base_idx + 1 < len(palavras):
            return base_idx + 1, 0
        else:
            return None, None
    except ValueError:
        return 0, 0


def salvar_ultima_combinacao(arquivo="ultimo.txt", palavra_base="", palavra_completa1="", palavra_completa2=""):
    """Salva a combinação atual no arquivo (10 + 2)"""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    mnemonic = " ".join(palavras)
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(mnemonic)


def salvar_checkpoint(arquivo="checkpoint.txt", base_idx=0, palavra_base="",
                      contador_total=0, contador_validas=0, carteiras_com_saldo=0):
    """Salva checkpoint com estatísticas atuais"""
    with open(arquivo, 'w', encoding='utf-8') as f:
        f.write(f"Última palavra base testada: {base_idx + 1} ({palavra_base})\n")
        f.write(f"Total de combinações testadas: {contador_total}\n")
        f.write(f"Combinações válidas: {contador_validas}\n")
        f.write(f"Carteiras com saldo: {carteiras_com_saldo}\n")


def criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2):
    """Cria mnemonic com palavra_base repetida 10 vezes + duas palavras variáveis finais"""
    palavras = [palavra_base] * 10 + [palavra_completa1, palavra_completa2]
    return " ".join(palavras)


def validar_mnemonic(mnemonic):
    """Valida se o mnemonic é válido segundo BIP39"""
    try:
        return Bip39MnemonicValidator().IsValid(mnemonic)
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


def verificar_saldo_mempool(endereco):
    """Verifica saldo do endereço usando API da Mempool.space"""
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
    """Salva carteira com saldo no arquivo saldo.txt"""
    with open("saldo.txt", "a", encoding='utf-8') as f:
        f.write(f"Palavra Base: {palavra_base} (repetida 10x)\n")
        f.write(f"Palavras Finais: {palavra_completa1}, {palavra_completa2}\n")
        f.write(f"Mnemonic: {mnemonic}\n")
        f.write(f"Endereço: {info['address']}\n")
        f.write(f"Chave Privada (WIF): {info['wif']}\n")
        f.write(f"Chave Privada (HEX): {info['priv_hex']}\n")
        f.write(f"Chave Pública: {info['pub_compressed_hex']}\n")
        f.write("-" * 80 + "\n\n")
    print("🎉 CARTEIRA COM SALDO SALVA! 🎉")


def main():
    """Função principal com sistema de checkpoint baseado na última combinação"""
    # Carregar palavras BIP39
    try:
        palavras = carregar_palavras_bip39("bip39-words.txt")
        print(f"Carregadas {len(palavras)} palavras BIP39")
    except FileNotFoundError as e:
        print(e)
        return

    # Carregar última combinação testada (compatível 10+2)
    ultima_base, ultima_completa1, ultima_completa2, ultimo_mnemonic = carregar_ultima_combinacao("ultimo.txt")

    # Carregar estatísticas do checkpoint
    contador_total, contador_validas, carteiras_com_saldo = carregar_estatisticas_checkpoint("checkpoint.txt")

    print("Estatísticas carregadas:")
    print(f"  Total de combinações testadas: {contador_total}")
    print(f"  Combinações válidas: {contador_validas}")
    print(f"  Carteiras com saldo: {carteiras_com_saldo}")

    # Determinar ponto de partida
    if ultima_base and ultima_completa1 and ultima_completa2:
        print(f"Última combinação testada: {ultimo_mnemonic}")
        base_idx, completa_idx = encontrar_proxima_combinacao(palavras, ultima_base, ultima_completa2)
        if base_idx is None:
            print("Todas as combinações já foram testadas!")
            return
    else:
        print("Nenhum checkpoint encontrado, começando do início...")
        base_idx, completa_idx = 0, 0

    print(f"Continuando da posição: palavra base #{base_idx+1} ('{palavras[base_idx]}'), variação #{completa_idx+1}")

    print("\nIniciando teste de combinações BIP39...")
    print("Padrão: 10 palavras repetidas + 2 variáveis")
    print("Verificando saldo na Mempool.space (Timeout: 10s)")
    print("Carteiras com saldo serão salvas em saldo.txt")
    print("Última combinação salva em ultimo.txt")
    print("Checkpoint salvo em checkpoint.txt")
    print("Pressione Ctrl+C para parar\n")

    ultimo_salvamento = time.time()

    try:
        # Iterar a partir do ponto atual
        for i in range(base_idx, len(palavras)):
            palavra_base = palavras[i]

            # Determinar índice inicial para palavra completa
            start_j = completa_idx if i == base_idx else 0

            # percorre j até len-1 para usar j and j+1
            for j in range(start_j, len(palavras) - 1):
                palavra_completa1 = palavras[j]
                palavra_completa2 = palavras[j + 1]
                contador_total += 1

                # Criar mnemonic 10+2
                mnemonic = criar_mnemonic_repetido(palavra_base, palavra_completa1, palavra_completa2)

                # Salvar combinação atual no arquivo (10+2)
                salvar_ultima_combinacao("ultimo.txt", palavra_base, palavra_completa1, palavra_completa2)

                # Salvar checkpoint a cada 30 segundos ou 100 combinações
                tempo_atual = time.time()
                if tempo_atual - ultimo_salvamento > 30 or contador_total % 100 == 0:
                    salvar_checkpoint("checkpoint.txt", i, palavra_base,
                                      contador_total, contador_validas, carteiras_com_saldo)
                    ultimo_salvamento = tempo_atual

                # Exibir progresso a cada 100 combinações
                if contador_total % 100 == 0:
                    print(f"Testadas {contador_total} combinações | Última: {mnemonic}")

                # Validar mnemonic
                if validar_mnemonic(mnemonic):
                    contador_validas += 1

                    # Gerar carteira
                    seed = mnemonic_para_seed(mnemonic)
                    addr_index = derivar_bip44_btc(seed)
                    info = mostrar_info(addr_index)

                    # Verificar saldo
                    tem_saldo = verificar_saldo_mempool(info["address"])

                    # Exibir progresso a cada 100 combinações válidas
                    if contador_validas % 100 == 0:
                        print(f"\nProgresso: {contador_validas} combinações válidas testadas")
                        print(f"Última válida: {mnemonic}")
                        print(f"Endereço: {info['address']}")
                        print(f"Saldo: {'SIM' if tem_saldo else 'NÃO'}")
                        print("-" * 50)

                    # Se tem saldo, salvar
                    if tem_saldo:
                        carteiras_com_saldo += 1
                        salvar_carteira_com_saldo(palavra_base, palavra_completa1, palavra_completa2, mnemonic, info)

                    # Aguardar para não sobrecarregar a API
                    time.sleep(0.1)

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
        if 'i' in locals() and 'palavra_base' in locals():
            salvar_checkpoint("checkpoint.txt", i, palavra_base,
                              contador_total, contador_validas, carteiras_com_saldo)

    finally:
        # Salvar estatísticas finais
        with open("estatisticas_finais.txt", "w", encoding='utf-8') as f:
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
            print(f"Taxa de sucesso: {(carteiras_com_saldo/contador_validas)*100:.8f}%")
        else:
            print("Taxa de sucesso: 0%")

if __name__ == "__main__":
    main()
