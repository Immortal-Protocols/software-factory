#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$project_dir/.venv/bin/python" "$project_dir/control.py" start
