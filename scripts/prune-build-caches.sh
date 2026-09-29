#!/usr/bin/env bash
# Delete the superbuild caches on main that the current tree can no longer hit.
#
#   scripts/prune-build-caches.sh VERSION INPUTS_HASH [--dry-run]
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
# Caches for a pull request live on refs/pull/N/merge and GitHub deletes them
# when the pull request closes, so they are never this script's business:
# deleting one would make a pull request that edits the build scripts rebuild
# cold on every push, which is exactly what dropping github.sha from the key
# avoided. Only refs/heads/main is listed.
#
# Run it with --dry-run to see what a push to main would delete. Needs a
# GH_TOKEN with `actions: write` for anything else.
set -euo pipefail

usage() {
  echo "usage: $0 VERSION INPUTS_HASH [--dry-run]" >&2
  exit 64
}

# Argument handling is strict because every way of getting it wrong ends in the
# same place: a suffix that matches nothing, and a run that deletes every live
# entry. `--dry-run 0.18.1 abc123` — the flag-first spelling of an interface
# that takes the flag last — would otherwise make "--dry-run" the version and
# delete the caches it was asked to preview.
[[ $# -ge 2 && $# -le 3 ]] || usage
case "${3:-}" in
  "") dry_run="" ;;
  --dry-run) dry_run=1 ;;
  *) usage ;;
esac

version="$1"
inputs_hash="$2"
# Empty is the input CI can actually produce: `hashFiles` returns an empty
# string when it matches no file. A leading dash is a misplaced flag.
if [[ -z "$version" || -z "$inputs_hash" ]] \
  || [[ "$version" == -* || "$inputs_hash" == -* ]]; then
  usage
fi

current_suffix="-$version-$inputs_hash"
echo "keeping every superbuild cache ending in $current_suffix"

# --limit is well above what three platforms across a few versions produce.
# It matters that it is: `gh cache list` sorts by last accessed, so a truncated
# listing drops the least recently read entries — exactly the orphans this
# exists to delete.
listing=$(gh cache list --ref refs/heads/main --key superbuild- --limit 1000 \
  --json id,key --jq '.[] | [.id, .key] | @tsv')

kept=0
stale=()
while IFS=$'\t' read -r id key; do
  [[ -n "$key" ]] || continue
  if [[ "$key" == *"$current_suffix" ]]; then
    echo "keeping $key"
    kept=$((kept + 1))
  else
    stale+=("$id"$'\t'"$key")
  fi
done <<<"$listing"

# The version and the hash are both *derived* on the runner rather than read
# back off the caches, so a value that is wrong but non-empty would make every
# live entry look stale in one unattended run. This job runs only after the
# whole wheel matrix succeeded, so at least one entry for the current tree must
# exist; none means the derivation is wrong, not that everything is orphaned.
if (( kept == 0 )); then
  echo "::error::no cache matches $current_suffix, so the version or the input hash is wrong; refusing to delete ${#stale[@]} entries"
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
