#!/bin/sh
# Run all tests. No root, no network. Everything happens in temp directories.
set -eu
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -v -s test -p 'test_*.py'
