#!/bin/bash
# ============================================
# 🚀 Sincronizador Automático Local ↔ GitHub
# Autor: ChatGPT (para Lucas 😎)
# ============================================

# CONFIGURAÇÃO
REPO_DIR="/c/Users/Pc/Downloads/realfindbitcoin"
REMOTE_URL="https://github.com/lucasandre16112000-png/btcpalavrasdiferentes2.git"
BRANCH="main"

echo "============================================"
echo "🔄 Sincronizando pasta local com GitHub..."
echo "📂 Pasta: $REPO_DIR"
echo "🌐 Repositório remoto: $REMOTE_URL"
echo "============================================"

# Ir para o diretório do repositório
cd "$REPO_DIR" || { echo "❌ ERRO: Pasta não encontrada!"; exit 1; }

# Inicializa Git se ainda não existir
if [ ! -d ".git" ]; then
  echo "🧱 Inicializando repositório Git..."
  git init
  git remote add origin "$REMOTE_URL"
  git branch -M "$BRANCH"
else
  echo "✅ Repositório Git já inicializado."
fi

# Atualiza com o que está no GitHub (caso exista)
echo "⬇️  Atualizando conteúdo remoto..."
git fetch origin "$BRANCH"
git pull origin "$BRANCH" --allow-unrelated-histories || echo "⚠️ Nenhuma atualização remota encontrada."

# Adiciona todas as mudanças
echo "📦 Adicionando arquivos alterados..."
git add .

# Cria commit automático com data/hora
COMMIT_MSG="Sincronização automática em $(date '+%d/%m/%Y %H:%M:%S')"
echo "📝 Criando commit: $COMMIT_MSG"
git commit -m "$COMMIT_MSG" || echo "⚠️ Nenhuma modificação para commitar."

# Envia pro GitHub
echo "🚀 Enviando alterações para o GitHub..."
git push origin "$BRANCH"

echo "✅ Sincronização concluída com sucesso!"
echo "============================================"
