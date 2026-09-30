#!/usr/bin/env bash
# Сканирование секретов. Одна точка входа для pre-commit хука, make и CI.
#
# Две независимые проверки:
#   1. .env-файлы под контролем версий (.env.example и прочие шаблоны разрешены)
#   2. gitleaks по содержимому — правила в .gitleaks.toml
#
# Режимы:
#   --staged    только staged-изменения (pre-commit хук)
#   --history   вся история коммитов (make secrets, CI) — режим по умолчанию
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

mode="${1:---history}"
case "$mode" in
  --staged)  scope="--staged"; what="staged-изменения" ;;
  --history) scope="";         what="история коммитов" ;;
  *) echo "usage: $0 [--staged|--history]" >&2; exit 2 ;;
esac

# 1. Файлы, которые не должны попадать в git вообще
if [ "$mode" = "--staged" ]; then
  files=$(git diff --cached --name-only --diff-filter=ACMR)
else
  files=$(git ls-files)
fi
env_files=$(printf '%s\n' "$files" \
  | grep -E '(^|/)\.env($|\.)' \
  | grep -Ev '\.(example|sample|template|dist)$' || true)
if [ -n "$env_files" ]; then
  echo "Эти файлы не должны быть под контролем версий:" >&2
  printf '  %s\n' $env_files >&2
  echo "Убери из индекса (git rm --cached <файл>) и внеси в .gitignore." >&2
  exit 1
fi

# 2. gitleaks
if ! command -v gitleaks >/dev/null 2>&1; then
  cat >&2 <<'MSG'
gitleaks не установлен — проверка содержимого не выполнена.
  macOS: brew install gitleaks
  иначе: https://github.com/gitleaks/gitleaks/releases
MSG
  exit 127
fi

echo "gitleaks: $what"
if ! gitleaks git . $scope --config .gitleaks.toml --redact --no-banner; then
  echo >&2
  echo "Найдены секреты. Если это ложное срабатывание — добавь правило" >&2
  echo "в allowlist в .gitleaks.toml или строчный комментарий gitleaks:allow." >&2
  exit 1
fi
