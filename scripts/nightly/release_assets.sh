#!/usr/bin/env bash
# Release assets for the workflows (.github/workflows/compat.yml and atlas.yml), paged through the REST API so a
# release with more than one page of assets is read in full.
#
#   release_assets.sh names TAG               every asset name, one per line (nothing when the release does not exist)
#   release_assets.sh download TAG DIR NAME...  the named assets into DIR (an unknown name is an error)
#
# Needs gh with GH_TOKEN and GITHUB_REPOSITORY (both set on GitHub runners).
set -euo pipefail

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
cmd="${1:?usage: release_assets.sh names TAG | download TAG DIR NAME...}"
tag="${2:?missing TAG}"

# "id name" per asset; nothing when the release does not exist (any other error stops the script)
list() {
  local id err
  err="$(mktemp)"
  if ! id="$(gh api "repos/$repo/releases/tags/$tag" --jq .id 2> "$err")"; then
    if grep -q "HTTP 404" "$err"; then
      rm -f "$err"
      return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  gh api --paginate "repos/$repo/releases/$id/assets?per_page=100" --jq '.[] | "\(.id) \(.name)"'
}

case "$cmd" in
  names)
    list | cut -d' ' -f2-
    ;;
  download)
    dir="${3:?missing DIR}"
    shift 3
    mkdir -p "$dir"
    declare -A ids=()
    assets="$(list)"  # not a process substitution, so a failed listing stops the script
    while read -r id name; do
      [ -n "$id" ] && ids["$name"]="$id"
    done <<< "$assets"
    for name in "$@"; do
      if [ -z "${ids[$name]:-}" ]; then
        echo "release $tag has no asset $name" >&2
        exit 1
      fi
      case "$name" in */* | .* ) echo "refusing asset name $name" >&2; exit 1 ;; esac
      gh api -H "Accept: application/octet-stream" "repos/$repo/releases/assets/${ids[$name]}" > "$dir/$name"
    done
    ;;
  *)
    echo "unknown command $cmd" >&2
    exit 2
    ;;
esac
