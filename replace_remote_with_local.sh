#!/bin/bash
usage() {
  echo "Uso: $0 [-y] [-u <remote_url>] [remote] [branch]"
  echo
  echo "  -y    Confirma automaticamente o push forçado (sem pedir)"
  echo "  -u    Define ou atualiza a URL do remote (ex: -u https://github.com/usuario/repositorio.git)"
  echo
  echo "Exemplo:"
  echo "  ./replace_remote_with_local.sh -y origin main"
  echo "  ./replace_remote_with_local.sh -u https://github.com/user/repo.git origin main"
  exit 1
}

AUTO_ACCEPT=0
SET_REMOTE_URL=""
while getopts ":yu:" opt; do
  case $opt in
    y) AUTO_ACCEPT=1 ;;
    u) SET_REMOTE_URL="$OPTARG" ;;
    \?) echo "Opção inválida -$OPTARG" >&2; usage ;;
  esac
done
shift $((OPTIND-1))

REMOTE="${1:-origin}"
BRANCH="${2:-main}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ Este diretório não é um repositório git. Abra o Git Bash na pasta do projeto."
  exit 2
fi

echo "📂 Repositório Git detectado. Remote alvo: '$REMOTE'  |  Branch: '$BRANCH'"

if [ -n "$SET_REMOTE_URL" ]; then
  if git remote | grep -q "^${REMOTE}$"; then
    echo "🔁 Atualizando remote $REMOTE -> $SET_REMOTE_URL"
    git remote set-url "$REMOTE" "$SET_REMOTE_URL"
  else
    echo "➕ Adicionando remote $REMOTE -> $SET_REMOTE_URL"
    git remote add "$REMOTE" "$SET_REMOTE_URL"
  fi
fi

echo "💾 Adicionando e commitando mudanças locais..."
git add --all
if git diff --cached --quiet; then
  echo "Nenhuma modificação nova detectada — criando commit vazio."
  git commit --allow-empty -m "force-sync: $(date --iso-8601=seconds)"
else
  git commit -m "auto-sync: $(date --iso-8601=seconds)"
fi

CURRENT_HEAD=$(git rev-parse --verify HEAD)
REMOTE_URL=$(git remote get-url "$REMOTE" 2>/dev/null || echo "(não definido)")

echo
echo "Resumo:"
echo "  Remote: $REMOTE -> $REMOTE_URL"
echo "  Branch: $BRANCH"
echo "  HEAD:   $CURRENT_HEAD"
echo

if [ "$AUTO_ACCEPT" -ne 1 ]; then
  read -p "⚠️  Confirma o PUSH FORÇADO para substituir tudo em '$REMOTE/$BRANCH'? (s/N) " ans
  case "$ans" in
    [sS]|[yY]) ;;
    *) echo "Operação cancelada."; exit 0 ;;
  esac
fi

echo "🚀 Fazendo push forçado..."
git push --force "$REMOTE" "HEAD:$BRANCH" || { echo "❌ Erro no push"; exit 3; }
echo "✅ Push concluído com sucesso!"
