# Shared helpers for the scripts that exercise a built wheel. Sourced, not run.
#
# Both callers work from a temporary directory outside the repository: a source
# checkout's palais_solver package shadows the installed one, and these tests
# must exercise what the wheel ships.

# make_wheel_venv VENV WHEEL [REQUIREMENT...]
#
# Create a virtual environment holding the wheel and nothing else, unless
# further requirements are named.
make_wheel_venv() {
  local venv="$1"
  shift
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet "$@"
}

# copy_example CONFIG DESTINATION
#
# Copy the example directory holding CONFIG somewhere writable. Palace resolves
# the mesh path relative to the working directory and writes its output beside
# the config, so a run needs its own copy.
copy_example() {
  cp -r "$(dirname "$1")" "$2"
  chmod -R u+w "$2"
}
