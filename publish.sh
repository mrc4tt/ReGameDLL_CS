#!/bin/bash
# Build ReGameDLL and collect release files into publish/, matching the
# Linux CI job in .github/workflows/build.yml.
#
# Usage: ./publish.sh [build.sh options]   e.g. ./publish.sh -j=8
#        ./publish.sh --no-build           reuse existing build/

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! "$*" =~ "--no-build" ]]; then
	./build.sh "$@"
fi

SO="build/regamedll/cs.so"
if [ ! -f "$SO" ]; then
	echo "Error: $SO not found. Build first (drop --no-build)."
	exit 1
fi

rm -rf publish
mkdir -p publish/bin/linux32/cstrike/dlls
mkdir -p publish/cssdk

# CSSDK headers
rsync -a regamedll/extra/cssdk/ publish/cssdk/ \
	--exclude=.git --exclude=LICENSE --exclude=README.md

# Binary + config + version
cp "$SO" publish/bin/linux32/cstrike/dlls/cs.so
cp regamedll/version/appversion.h publish/appversion.h
cp -r dist publish/dist

# Stage runnable layout for a local HLDS: cs.so + dist next to it
rsync -a dist/ publish/bin/linux32/cstrike/

echo "Done. Release files in publish/"
echo "  Local test: copy publish/bin/linux32/cstrike/* into your HLDS cstrike/"
