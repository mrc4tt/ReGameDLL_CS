#!/bin/bash
# Runs INSIDE docker/Dockerfile. Mirrors the linux job of build.yml:
# unittests, release build, cssdk + cs.so + dist into publish/, glibc check.
set -euo pipefail
git config --global --add safe.directory '*'

echo "==> Unittests build"
rm -rf build
CC=gcc CXX=g++ cmake -DCMAKE_BUILD_TYPE=Unittests -B build
cmake --build build -j"$(nproc)"
retVal=0
./build/regamedll/cs 2>/dev/null > build/unittests.log || retVal=$?
grep -E "Failure in test|Warning in test|Tests executed" build/unittests.log || true
# CI contract: exit code 0 or 3 = pass
if [ "$retVal" -ne 0 ] && [ "$retVal" -ne 3 ]; then
	echo "Unittests FAILED (exit $retVal)"; exit 1
fi

echo "==> Release build"
rm -rf build
CC=gcc CXX=g++ cmake -B build
cmake --build build -j"$(nproc)"

echo "==> publish/ (linux)"
rm -rf publish/bin/linux32 publish/cssdk publish/dist publish/appversion.h
mkdir -p publish/cssdk publish/bin/linux32/cstrike/dlls
rsync -a regamedll/extra/cssdk/ publish/cssdk/ --exclude=.git --exclude=LICENSE --exclude=README.md
cp build/regamedll/cs.so publish/bin/linux32/cstrike/dlls/cs.so
cp regamedll/version/appversion.h publish/appversion.h
rsync -a dist/ publish/dist/

echo "==> GLIBC/ABI compat test"
bash ./regamedll/version/glibc_test.sh publish/bin/linux32/cstrike/dlls/cs.so
