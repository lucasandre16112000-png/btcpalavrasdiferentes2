#!/bin/bash
# ============================================
# 🚀 Sincronizador Automático Local ↔ GitHub
# VERSÃO CORRIGIDA (com git stash e .gitignore)
# ============================================

# --- CONFIGURAÇÃO ---
# Usa o diretório onde o script está
REPO_DIR=$(pwd)
REMOTE_URL="https://github.com/lucasandre16112000-png/btcpalavrasdiferentes2.git"
BRANCH="main"

echo "============================================"
echo "🔄 Sincronizando pasta local com GitHub..."
echo "📂 Pasta: $REPO_DIR"
echo "🌐 Repositório remoto: $REMOTE_URL"
echo "============================================"

# Ir para o diretório (garantia)
cd "$REPO_DIR" || { echo "❌ ERRO: Pasta não encontrada!"; exit 1; }

# --- Bloco de Correção .gitignore ---
# Garante que o .gitignore exista e ignore arquivos de progresso e ambiente
GITIGNORE_FILE=".gitignore"
echo "Verificando .gitignore..."

# Regras que DEVEM estar no .gitignore
RULES_TO_ADD=(
    "meu_ambiente_btc/"
    "checkpoint.txt"
    "ultimo.txt"
    "saldo.txt"
    "estatisticas_finais.txt"
    "venv/"
    ".venv/"
    "_pycache_/"
    "*.log"
    "*.pyc"
    "*.swp"
    "*.bak"
)

# Adiciona regras que estão faltando
touch $GITIGNORE_FILE
for rule in "${RULES_TO_ADD[@]}"; do
    if ! grep -qxF "$rule" "$GITIGNORE_FILE"; then
        echo "Adicionando regra: $rule"
        echo "$rule" >> "$GITIGNORE_FILE"
    fi
done

# Limpa o cache do Git (se a pasta do ambiente foi adicionada por engano antes)
git rm -r --cached meu_ambiente_btc/ > /dev/null 2>&1 || true
# --- Fim do Bloco de Correção ---


# 1. Salva seu progresso local (checkpoint.txt, etc.)
echo "💾 Salvando progresso local (stash)..."
git stash save "Progresso local antes da sincronização"

# 2. Puxa as atualizações do GitHub
echo "⬇️  Puxando atualizações do GitHub..."
git pull origin "$BRANCH" --rebase

# 3. Restaura seu progresso local
echo "🔄 Restaurando progresso local (stash pop)..."
git stash pop || echo "⚠️ Nenhum progresso local para restaurar."

# 4. Adiciona TODAS as mudanças (incluindo o .gitignore se foi alterado)
echo "📦 Adicionando arquivos alterados..."
git add .

# 5. Cria o commit
COMMIT_MSG="Sincronização automática em $(date '+%d/%m/%Y %H:%M:%S')"
echo "📝 Tentando criar commit: $COMMIT_MSG"
# || true garante que o script continue mesmo se não houver nada para commitar
git commit -m "$COMMIT_MSG" || true 

# 6. Envia (Push) para o GitHub
echo "🚀 Enviando alterações para o GitHub..."
git push origin "$BRANCH"

if [ $? -eq 0 ]; then
    echo "✅ Sincronização PULL e PUSH concluída com sucesso!"
else
    echo "🔴 ERRO NO PUSH: Ocorreu um erro ao enviar para o GitHub. Verifique as mensagens acima."
fi
echo "============================================"