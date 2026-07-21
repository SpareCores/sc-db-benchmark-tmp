#!/bin/sh
# Pull run3 results from benchmark hosts into ../run3/<sku>/
set -e
cd "$(dirname "$0")/.."
mkdir -p run3

rsync -az --delete ubuntu@172.196.52.21:/bench/run3/Standard_F16ams_v6/ run3/Standard_F16ams_v6/
rsync -az --delete ubuntu@172.204.26.67:/bench/run3/Standard_E16as_v6/ run3/Standard_E16as_v6/
rsync -az --delete ubuntu@172.204.25.45:/bench/run3/Standard_F32ams_v6/ run3/Standard_F32ams_v6/
