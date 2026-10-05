#!/usr/bin/env bash
# Delete the superbuild caches on main that the current tree can no longer hit.
#
#   scripts/prune-build-caches.sh VERSION INPUTS_HASH WINDOWS_PATCHES_HASH [--dry-run]
#
# A version bump or an edit to any keyed build script orphans one entry per
# platform, and GitHub holds an unused entry for seven days. Left to
# least-recently-used eviction the entry dropped is the least recently *read*,
# so the platform that builds least often loses its cache and pays a cold
# build. This deletes the orphans instead, and nothing else.
#
# What is kept is every key the *current* tree would produce, on any platform —
# not the one key this machine would produce. The wheel job's key is
# `superbuild-<platform tag>-<version>-<hash of the build scripts>`, so the two
# trailing components identify the tree and the leading one identifies the
# platform that built it. Matching on the trailing components therefore keeps
# one live entry per matrix row without this script holding a list of rows that
# could drift from `.github/workflows/wheels.yml`. Deriving the tag from the
# runner instead, which is what this did while the matrix had one row, would
# delete every other platform's live entry on every push to main.
#
# The one per-row difference is Windows, whose key ends in one more component,
# `-<hash of the carried patches>`, because only Windows carries patches. So a
# `superbuild-win_*` entry is live when it ends in all three components and
# every other entry when it ends in the two. That is a rule about the form of a
# key rather than a list of rows: it holds for any future Windows row, and it
# deletes a Windows entry saved before the patches were keyed, which no current
# run can restore exactly and which would otherwise never be replaced.
#
# Only main saves superbuild caches (see "Save build cache" in
# .github/workflows/wheels.yml). A pull request, a branch dispatch or a tag
# restores main's entry, rebuilds from it what its own tree changed, and saves
# nothing, so a pull request that edits the build scripts rebuilds from main's
# fallback on every push rather than cold, and leaves nothing here to prune.
# Only refs/heads/main is listed.
#
# Run it with --dry-run to see what a push to main would delete. Needs a
# GH_TOKEN with `actions: write` for anything else.
set -euo pipefail

usage() {
  echo "usage: $0 VERSION INPUTS_HASH WINDOWS_PATCHES_HASH [--dry-run]" >&2
  exit 64
}

# Argument handling is strict because every way of getting it wrong ends in the
# same place: a suffix that matches nothing, and a run that deletes every live
# entry. `--dry-run 0.18.1 abc123 def456` — the flag-first spelling of an
# interface that takes the flag last — would otherwise make "--dry-run" the
# version and delete the caches it was asked to preview.
[[ $# -ge 3 && $# -le 4 ]] || usage
case "${4:-}" in
  "") dry_run="" ;;
  --dry-run) dry_run=1 ;;
  *) usage ;;
esac

version="$1"
inputs_hash="$2"
windows_patches_hash="$3"
# Empty is the input CI can actually produce: `hashFiles` returns an empty
# string when it matches no file. A leading dash is a misplaced flag.
for value in "$version" "$inputs_hash" "$windows_patches_hash"; do
  [[ -n "$value" && "$value" != -* ]] || usage
done

current_suffix="-$version-$inputs_hash"
windows_suffix="$current_suffix-$windows_patches_hash"
echo "keeping every superbuild-win_* cache ending in $windows_suffix"
echo "keeping every other superbuild cache ending in $current_suffix"

# --limit is well above what three platforms across a few versions produce.
# It matters that it is: `gh cache list` sorts by last accessed, so a truncated
# listing drops the least recently read entries — exactly the orphans this
# exists to delete.
listing=$(gh cache list --ref refs/heads/main --key superbuild- --limit 1000 \
  --json id,key --jq '.[] | [.id, .key] | @tsv')

kept=0
windows_seen=0
windows_kept=0
stale=()
while IFS=$'\t' read -r id key; do
  [[ -n "$key" ]] || continue
  suffix="$current_suffix"
  if [[ "$key" == superbuild-win_* ]]; then
    suffix="$windows_suffix"
    windows_seen=$((windows_seen + 1))
  fi
  if [[ "$key" == *"$suffix" ]]; then
    echo "keeping $key"
    if [[ "$suffix" == "$windows_suffix" ]]; then
      windows_kept=$((windows_kept + 1))
    else
      kept=$((kept + 1))
    fi
  else
    stale+=("$id"$'\t'"$key")
  fi
done <<<"$listing"

# The version and the hashes are all *derived* on the runner rather than read
# back off the caches, so a value that is wrong but non-empty would make every
# live entry look stale in one unattended run. This job runs only after the
# whole wheel matrix succeeded, so an entry for the current tree must exist on
# every row; none means the derivation is wrong, not that everything is
# orphaned. The Windows check is its own because the Windows hash is: a wrong
# one would leave the other rows' entries matching and delete only the live
# Windows entry, which costs the most to rebuild.
if (( kept == 0 )); then
  echo "::error::no cache matches $current_suffix, so the version or the input hash is wrong; refusing to delete ${#stale[@]} entries"
  exit 1
fi
if (( windows_seen > 0 && windows_kept == 0 )); then
  echo "::error::no superbuild-win_* cache matches $windows_suffix, so the patches hash is wrong; refusing to delete ${#stale[@]} entries"
  exit 1
fi

failures=0
for entry in ${stale[@]+"${stale[@]}"}; do
  id="${entry%%$'\t'*}"
  key="${entry#*$'\t'}"
  if [[ -n "$dry_run" ]]; then
    echo "would delete stale $key"
    continue
  fi
  echo "deleting stale $key"
  # One entry a concurrent run already deleted must not strand the others: the
  # whole point is to leave main holding only current entries, and abandoning
  # the loop halfway leaves the orphans this was invoked to remove.
  if ! gh cache delete "$id"; then
    echo "::warning::could not delete $key ($id)"
    failures=$((failures + 1))
  fi
done

if (( failures > 0 )); then
  echo "::error::$failures cache(s) could not be deleted"
  exit 1
fi
