#!/usr/bin/env bash
# Cria um arquivo de swap de 2 GB em /swapfile.
# Recomendado em e2-micro (1 GB RAM) para evitar OOM ao rodar Docker.
# Idempotente: se o swap já existir e estiver ativo, sai sem erro.
#
# Uso (na VM):
#   sudo ./scripts/setup-swap.sh
# Opcional: tamanho em GB como primeiro argumento (default 2).
#   sudo ./scripts/setup-swap.sh 4

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "rode como root: sudo $0 $*" >&2
  exit 1
fi

SIZE_GB="${1:-2}"
SWAPFILE="/swapfile"

if swapon --show | grep -q "$SWAPFILE"; then
  echo "swap já ativo em $SWAPFILE:"
  swapon --show
  exit 0
fi

if [[ -e "$SWAPFILE" ]]; then
  echo "$SWAPFILE já existe mas não está ativo. Removendo para recriar." >&2
  rm -f "$SWAPFILE"
fi

echo "criando $SWAPFILE com ${SIZE_GB} GB..."
fallocate -l "${SIZE_GB}G" "$SWAPFILE" || dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((SIZE_GB * 1024)) status=progress
chmod 600 "$SWAPFILE"
mkswap "$SWAPFILE"
swapon "$SWAPFILE"

if ! grep -qE "^$SWAPFILE\s" /etc/fstab; then
  echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

if ! grep -q '^vm.swappiness' /etc/sysctl.conf; then
  echo 'vm.swappiness=10' >> /etc/sysctl.conf
  sysctl vm.swappiness=10 >/dev/null
fi

echo "swap ativo:"
swapon --show
echo "memória:"
free -h
