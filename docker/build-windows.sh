#!/bin/bash
# Windows build, mirroring the windows job of build.yml with support/vcxbuild.py
# (cl/link from msvc-wine) in place of msbuild. Two steps, two images:
#
#   build-windows.sh compile   in Dockerfile.windows (msvc-wine): Tests, Release, Release Play
#   build-windows.sh finish    in Dockerfile.wine32: unittests under Wine, publish/, rcedit
#                              (needs COMMIT in the environment)
set -euo pipefail
SLN=msvc/ReGameDLL.sln
PROJ=regamedll/msvc/ReGameDLL.vcxproj
owner=$(stat -c %u:%g .)

case "${1:-}" in
compile)
	git config --global --add safe.directory '*'
	regamedll/version/appversion.sh . >/dev/null
	rm -rf build-win
	for cfg in Tests Release "Release Play"; do
		echo "==> $cfg"
		python3 support/vcxbuild.py "$PROJ" --sln "$SLN" --config "$cfg" --out build-win
	done
	chown -R "$owner" build-win regamedll/version/appversion.h
	;;
finish)
	APP_VERSION=$(sed -n 's/^#define APP_VERSION_STRD[[:space:]]*"\([^"]*\)".*/\1/p' regamedll/version/appversion.h)
	echo "==> Windows unittests"
	retVal=0
	wine build-win/Tests/mp.exe > build-win/unittests.log 2>&1 || retVal=$?
	grep -E "Failure in test|Warning in test|Tests executed" build-win/unittests.log || true
	if [ "$retVal" -ne 0 ] && [ "$retVal" -ne 3 ]; then
		echo "Windows unittests FAILED (exit $retVal)"; tail -20 build-win/unittests.log; exit 1
	fi

	echo "==> publish/ (win32)"
	rm -rf publish/bin/win32 publish/tests publish/debug
	mkdir -p publish/bin/win32/cstrike/dlls publish/tests publish/debug
	cp build-win/Release/mp.dll publish/bin/win32/cstrike/dlls/mp.dll
	cp build-win/Release/mp.pdb publish/debug/mp.pdb
	cp build-win/Release_Play/mp.dll publish/tests/mp.dll

	echo "==> rcedit $APP_VERSION"
	wine /opt/rcedit-x86.exe publish/bin/win32/cstrike/dlls/mp.dll \
		--set-version-string ProductName "Regamedll_CS - mp.dll" \
		--set-file-version "$APP_VERSION" --set-product-version "$APP_VERSION" \
		--set-version-string FileDescription "Regamedll_CS (mp.dll) - provide more stable (than official) version of Counter-Strike game with extended API for mods and plugins, Commit: $COMMIT" \
		--set-version-string Comments "Regamedll_CS is a result of reverse engineering of original library mod HLDS (build 6153beta) using DWARF debug info embedded into linux version of HLDS, cs.so. Commit: $COMMIT" \
		--set-version-string CompanyName "ReHLDS Dev Team" \
		--set-version-string LegalCopyright "Copyright 2025 Valve, ReHLDS DevTeam" \
		--set-icon regamedll/msvc/icon.ico
	chown -R "$owner" build-win publish
	;;
*) echo "usage: build-windows.sh compile|finish" >&2; exit 2 ;;
esac
