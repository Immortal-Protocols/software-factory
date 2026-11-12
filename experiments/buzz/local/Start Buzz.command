#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$project_dir/buzz.sh" start
open -a "/Applications/Buzz Local.app"
