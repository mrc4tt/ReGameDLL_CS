#!/usr/bin/env bash
# Local release for ReGameDLL_CS: replaces the GitHub Actions build/release.
# Runs on the RE server through `server-ops release --only ReGameDLL_CS` (ops.miksen.me ->
# Releases), but works the same on any machine with docker, git, gh and the
# msvc-wine:latest image (on RE: `server-ops msvc-image`).
#
#   support/local-release.sh --versions
#       one TSV line for server-ops: current, changes, auto, patch, minor, major
#   support/local-release.sh [--bump patch|minor|major | --version X.Y.Z] [--dry-run]
#       build Linux (docker/Dockerfile: unittests, cs.so, glibc check) and Windows
#       (docker/Dockerfile.windows: msvc-wine + support/vcxbuild.py; Dockerfile.wine32: unittests under
#       Wine, mp.dll, rcedit), then bump regamedll/version/version.h, commit
#       "bump X.Y.Z", tag vX.Y.Z, push, and publish the GitHub release with
#       regamedll-bin-<APP_VERSION>.zip (bin/ + cssdk/, like upstream).
#       --dry-run builds and packages but changes nothing; --no-windows skips Windows.
#
# If version.h already holds a version that has no tag yet (bumped by hand),
# that version is released as is, without another bump.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
VERSION_FILE=regamedll/version/version.h
BRANCH=master
REMOTE=origin
IMAGE=regamedll-build:debian11
WIN_IMAGE=regamedll-build:msvc-wine
WINE_IMAGE=regamedll-build:wine32
NAME=ReGameDLL_CS

die() { echo "local-release: $*" >&2; exit 1; }
vget() { sed -n "s/^#define[[:space:]]\+$1[[:space:]]\+\([0-9]\+\).*/\1/p" "$VERSION_FILE"; }

MAJOR=$(vget VERSION_MAJOR); MINOR=$(vget VERSION_MINOR); MAINT=$(vget VERSION_MAINTENANCE)
[[ -n "$MAJOR" && -n "$MINOR" && -n "$MAINT" ]] || die "cannot parse $VERSION_FILE"
FILE_VER="$MAJOR.$MINOR.$MAINT"
LAST_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' HEAD 2>/dev/null || true)
LAST_VER=${LAST_TAG#v}
CHANGES=$([[ -n "$LAST_TAG" ]] && git rev-list --count "$LAST_TAG..HEAD" || echo "?")
tag_exists() { git rev-parse -q --verify "refs/tags/v$1" >/dev/null; }

next() { # next <patch|minor|major>  from the last released version
	local a b c; IFS=. read -r a b c <<< "${LAST_VER:-$FILE_VER}"
	case "$1" in
		patch) echo "$a.$b.$((c + 1))" ;;
		minor) echo "$a.$((b + 1)).0" ;;
		major) echo "$((a + 1)).0.0" ;;
	esac
}

if [[ "${1:-}" == --versions ]]; then
	# auto: a hand-bumped version.h wins, else nothing when there are no changes, else patch
	if ! tag_exists "$FILE_VER"; then auto="v$FILE_VER"
	elif [[ "$CHANGES" == 0 ]]; then auto=-
	else auto="v$(next patch)"; fi
	printf '%s\t%s\t%s\tv%s\tv%s\tv%s\n' "${LAST_TAG:--}" "$CHANGES" "$auto" "$(next patch)" "$(next minor)" "$(next major)"
	exit 0
fi

BUMP="" WANT="" DRY=0 WINDOWS=1
while (( $# )); do
	case "$1" in
		--bump)    [[ "${2:-}" =~ ^(patch|minor|major)$ ]] || die "bad --bump"; BUMP=$2; shift ;;
		--version) [[ "${2:-}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "bad --version (X.Y.Z)"; WANT=${2#v}; shift ;;
		--dry-run) DRY=1 ;;
		--no-windows) WINDOWS=0 ;;
		-h|--help) sed -n '2,20p' "$0"; exit 0 ;;
		*) die "unknown option: $1" ;;
	esac
	shift
done

# --- what to release ---------------------------------------------------------------
if [[ -n "$WANT" ]]; then NEW=$WANT
elif ! tag_exists "$FILE_VER"; then NEW=$FILE_VER          # hand-bumped, release as is
elif [[ "$CHANGES" == 0 && -z "$BUMP" ]]; then echo "$NAME: no changes since $LAST_TAG, skipped"; exit 0
else NEW=$(next "${BUMP:-patch}"); fi
TAG="v$NEW"
tag_exists "$NEW" && die "tag $TAG already exists"

# --- preflight ---------------------------------------------------------------------
[[ "$(git branch --show-current)" == "$BRANCH" ]] || die "not on $BRANCH"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "working tree has changes"
git fetch -q "$REMOTE" "$BRANCH" --tags
[[ "$(git rev-parse HEAD)" == "$(git rev-parse "$REMOTE/$BRANCH")" ]] || die "$BRANCH is not $REMOTE/$BRANCH"
command -v docker >/dev/null || die "docker missing"
(( ! WINDOWS )) || docker image inspect msvc-wine:latest >/dev/null 2>&1 || die "msvc-wine:latest missing (on RE: server-ops msvc-image), or use --no-windows"
git submodule update -q --init --recursive
(( DRY )) || gh auth status >/dev/null 2>&1 || die "gh is not logged in"
SLUG=$(git remote get-url "$REMOTE" | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')

echo "== $NAME: ${LAST_TAG:-none} -> $TAG ($CHANGES commits)$( (( DRY )) && echo ', dry run')"

# --- bump (local commit, undone if anything below fails) ----------------------------
START=$(git rev-parse HEAD) ARMED=0 NOTES=""
on_exit() {
	local rc=$?
	[[ -n "$NOTES" ]] && rm -f "$NOTES"
	if (( rc != 0 && ARMED )); then
		git reset -q --hard "$START"; git tag -d "$TAG" >/dev/null 2>&1 || true
		echo "local-release: failed, $BRANCH reset to ${START:0:8}" >&2
	fi
}
trap on_exit EXIT
(( DRY )) || ARMED=1
if [[ "$NEW" != "$FILE_VER" ]] && (( ! DRY )); then
	IFS=. read -r a b c <<< "$NEW"
	sed -i -e "s/^\(#define[[:space:]]\+VERSION_MAJOR[[:space:]]\+\)[0-9]\+/\1$a/" \
	       -e "s/^\(#define[[:space:]]\+VERSION_MINOR[[:space:]]\+\)[0-9]\+/\1$b/" \
	       -e "s/^\(#define[[:space:]]\+VERSION_MAINTENANCE[[:space:]]\+\)[0-9]\+/\1$c/" "$VERSION_FILE"
	git add "$VERSION_FILE"
	git commit -q -m "bump $NEW"
fi

# --- build (appversion.h = version.h + commit count, so after the bump commit) -------
echo "== linux: docker image $IMAGE"
docker build -q -t "$IMAGE" -f docker/Dockerfile docker >/dev/null
docker run --rm -v "$ROOT:/src" -w /src -u "$(id -u):$(id -g)" -e HOME=/tmp "$IMAGE"
if (( WINDOWS )); then
	echo "== windows: docker images $WIN_IMAGE, $WINE_IMAGE"
	docker build -q -t "$WIN_IMAGE" -f docker/Dockerfile.windows docker >/dev/null
	docker build -q -t "$WINE_IMAGE" -f docker/Dockerfile.wine32 docker >/dev/null
	docker run --rm -v "$ROOT:/src" -w /src "$WIN_IMAGE" bash docker/build-windows.sh compile
	docker run --rm -v "$ROOT:/src" -w /src -e COMMIT="$(git rev-parse HEAD)" "$WINE_IMAGE" bash docker/build-windows.sh finish
fi

APP_VERSION=$(sed -n 's/^#define APP_VERSION_STRD[[:space:]]*"\([^"]*\)".*/\1/p' publish/appversion.h)
BUILT=$( (( DRY )) && echo "$FILE_VER" || echo "$NEW")   # a dry run builds without the bump
[[ "$APP_VERSION" == "$BUILT".* ]] || die "appversion.h says '$APP_VERSION', expected $BUILT.*"

# Zip layout as upstream: dist/ (configs) into each cstrike dir, then bin/ + cssdk/.
STAGE=$(mktemp -d)
cp -r publish/bin publish/cssdk "$STAGE/"
for d in "$STAGE"/bin/*/cstrike; do cp -r publish/dist/. "$d/"; done
ZIP="$ROOT/publish/regamedll-bin-$APP_VERSION.zip"
rm -f publish/*.zip
(cd "$STAGE" && zip -qr "$ZIP" bin cssdk)
rm -rf "$STAGE"
echo "== packaged $(basename "$ZIP") ($(du -h "$ZIP" | cut -f1)): $(cd publish && ls bin/*/cstrike/dlls/* | tr '\n' ' ')"

if (( DRY )); then
	echo "== dry run: would tag $TAG, push $BRANCH + $TAG to $REMOTE and release $SLUG $TAG"
	exit 0
fi

# --- tag, push, release -------------------------------------------------------------
NOTES=$(mktemp)
{
	echo "## ReGameDLL_CS $NEW ($APP_VERSION)"
	echo
	if [[ -n "$LAST_TAG" ]]; then
		git log --no-merges --format='- %s (%h)' "$LAST_TAG..HEAD" | grep -v '^- bump [0-9]' || echo "- no changes"
	else
		echo "First release from this fork. Latest changes:"
		git log --no-merges -30 --format='- %s (%h)' HEAD | grep -v '^- bump [0-9]'
	fi
	(( WINDOWS )) || { echo; echo "Linux only (no Windows build)."; }
} > "$NOTES"

git tag -a "$TAG" -m "ReGameDLL_CS $NEW"
git push -q "$REMOTE" "$BRANCH"
ARMED=0                       # master is pushed: no undo from here on
git push -q "$REMOTE" "$TAG"
gh release create "$TAG" "$ZIP" -R "$SLUG" --title "$TAG" --notes-file "$NOTES" --latest
echo "== released https://github.com/$SLUG/releases/tag/$TAG"
