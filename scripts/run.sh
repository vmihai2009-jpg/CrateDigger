#!/bin/zsh
# Run CrateDigger from the project folder (logs also go to the terminal).
cd "$(dirname "$0")/.."
exec .venv/bin/python -m cratedigger "$@"
