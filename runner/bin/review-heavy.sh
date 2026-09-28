#!/bin/bash
exec python3 "$(dirname "$(readlink -f "$0")")/review_heavy.py" "$@"
