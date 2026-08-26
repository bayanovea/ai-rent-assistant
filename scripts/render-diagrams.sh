#!/usr/bin/env bash
# Render every docs/diagrams/*.mmd to the .svg next to it.
#
# The docs embed the SVGs rather than fenced mermaid blocks, because VS Code's
# built-in markdown preview does not render mermaid without an extension. The
# .mmd file is the source; run this after editing one.
#
# pipeline-flow.svg has no .mmd: it is drawn by gen-pipeline-diagram.py instead
# and is left alone here.
#
# Chromium comes from the Playwright cache the scraper already installed, so
# nothing is added to the system: npm packages go to a temp cache and are
# thrown away with it.
set -euo pipefail

cd "$(dirname "$0")/.."
diagrams="docs/diagrams"

find_chrome() {
    local cache="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
    [[ -d "$cache" ]] || cache="$HOME/.cache/ms-playwright"
    local candidate
    while IFS= read -r candidate; do
        [[ -x "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
    done < <(find "$cache" -maxdepth 7 \
        \( -name 'Google Chrome for Testing' -o -name 'chrome' -o -name 'Chromium' \) \
        -type f 2>/dev/null | sort -r)
    return 1
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

args=()
if chrome="$(find_chrome)"; then
    printf '{"executablePath": %s, "args": ["--no-sandbox"]}\n' \
        "$(printf '%s' "$chrome" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
        > "$tmp/puppeteer.json"
    args+=(-p "$tmp/puppeteer.json")
    echo "chromium: $chrome"
else
    echo "chromium: not found in the Playwright cache, letting puppeteer fetch its own" >&2
fi

shopt -s nullglob
for src in "$diagrams"/*.mmd; do
    out="${src%.mmd}.svg"
    echo "  $src -> $out"
    npx -y --cache "$tmp/npm" @mermaid-js/mermaid-cli@11 \
        "${args[@]}" -i "$src" -o "$out" -b white --quiet
done
echo "done"
