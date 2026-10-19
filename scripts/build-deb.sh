#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
[ "$(dpkg --print-architecture)" = riscv64 ] || { echo 'gateway deb 必须在 riscv64 构建' >&2; exit 1; }
# 仅由 bibit-dev gateway 配方调用；每次输出路径固定，摘要由平台生成。
dpkg-buildpackage -us -uc -b -Zxz
version=$(dpkg-parsechangelog -S Version)
package="../spacemit-ai-gateway_${version}_riscv64.deb"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
dpkg-deb -x "$package" "$tmp"
python3 debian/runtime.py validate "$tmp/opt/spacemit-ai-gateway/runtime" >/dev/null
cp "$package" spacemit-ai-gateway.deb
cp debian/tests/test-device-lifecycle.py test-device-lifecycle.py
