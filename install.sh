#!/bin/bash
set -eu

mega_install_tmp="$(mktemp -d)"
trap 'rm -rf "$mega_install_tmp"' EXIT HUP INT TERM

git clone --depth 1 \
  https://github.com/volodymyr-matyash/mega_hacs.git \
  "$mega_install_tmp/mega_hacs"
mkdir -p custom_components
cp -R "$mega_install_tmp/mega_hacs/custom_components/mega" custom_components/
