#!/usr/bin/env bash
# Release assets for the workflows (.github/workflows/compat.yml and atlas.yml), paged through the REST API so a
# release with more than one page of assets is read in full.
#
#   release_assets.sh names TAG               every asset name, one per line (nothing when the release does not exist)
#   release_assets.sh download TAG DIR NAME...  the named assets into DIR (an unknown name is an error)
#   release_assets.sh exists TAG              exit 0 when the release exists (a draft too), 1 when it does not,
#                                             2 when the lookup failed
#
# A draft release (atlas.yml keeps the Nasdaq days in one) has no tag yet, so `releases/tags/TAG` does not return it;
# it is found by its pending tag name in the release list, which a token with write access sees.
#
# Needs gh with GH_TOKEN and GITHUB_REPOSITORY (both set on GitHub runners).
set -euo pipefail

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
cmd="${1:?usage: release_assets.sh names TAG | download TAG DIR NAME... | exists TAG}"
tag="${2:?missing TAG}"
# the tag goes into a jq program below: only plain tag characters
if ! [[ "$tag" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "refusing tag $tag" >&2
  exit 2
fi

# the release id; nothing when the release does not exist (any other error stops the script)
release_id() {
  local id err
  err="$(mktemp)"
  if ! id="$(gh api "repos/$repo/releases/tags/$tag" --jq .id 2> "$err")"; then
    if grep -q "HTTP 404" "$err"; then
      rm -f "$err"
      # a draft: look it up by its pending tag name (a failed listing is an error, never "no release")
      local ids
      ids="$(gh api --paginate "repos/$repo/releases?per_page=100" \
        --jq ".[] | select(.draft and .tag_name == \"$tag\") | .id")" || return 1
      head -n 1 <<< "$ids"
      return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  echo "$id"
}

# "id name" per asset; nothing when the release does not exist (any other error stops the script)
list() {
  local id
  id="$(release_id)" || return 1
  [ -n "$id" ] || return 0
  gh api --paginate "repos/$repo/releases/$id/assets?per_page=100" --jq '.[] | "\(.id) \(.name)"'
}

case "$cmd" in
  names)
    list | cut -d' ' -f2-
    ;;
  exists)
    id="$(release_id)" || exit 2
    [ -n "$id" ]
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
