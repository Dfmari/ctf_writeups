#!/bin/bash

echo "[*] Initializing deployment pipeline..."

# 1. Universal Asset Fix (Fixes Obsidian relative link structure)
echo "[*] Patching media links in exported HTML..."
find writeups -name "*.html" -exec sed -i '/href="\.\.\/\.\.\/\.\.\/media\// s/class="internal-link is-unresolved"/class="external-link"/g' {} +
find writeups -name "*.html" -exec sed -i '/href="\.\.\/\.\.\/\.\.\/media\// s/class="internal-link"/class="external-link"/g' {} +

# 2. FONT INJECTION ONLY
# This finds </head> and inserts a style block that defines the font face and forces body/code to use it.
# It uses the font hosted on your main site to ensure it loads everywhere.
echo "[*] Injecting BigBlueTerm font..."
find writeups -name "*.html" -exec sed -i 's|</head>|<style>@font-face{font-family:"BigBlueTerm";src:url("../../../media/fonts/BigBlueTerm.ttf") format("truetype");font-display:swap;} body, code, pre, .markdown-preview-view { font-family: "BigBlueTerm", monospace !important; -webkit-font-smoothing: none !important; }</style></head>|g' {} +

# 3. Stage changes
echo "[*] Staging files..."
git add .

# 4. Commit
TMP_MSG=$(mktemp /tmp/git-commit-msg-XXXXXX.txt)
echo "" > "$TMP_MSG"
echo "# Write your commit message above." >> "$TMP_MSG"
nvim "$TMP_MSG"

CLEAN_MSG=$(grep -v '^#' "$TMP_MSG" | sed '/^\s*$/d')
if [ -z "$CLEAN_MSG" ]; then
    echo "[!] Empty commit message. Aborting."
    rm "$TMP_MSG"
    exit 1
fi

git commit -F "$TMP_MSG"
rm "$TMP_MSG"

# 5. Push
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
read -p "[?] Push to which branch? [Enter for '$CURRENT_BRANCH']: " TARGET_BRANCH
if [ -z "$TARGET_BRANCH" ]; then
    TARGET_BRANCH=$CURRENT_BRANCH
fi

echo "[*] Pushing to origin/$TARGET_BRANCH..."
git push origin "$TARGET_BRANCH"

echo "[*] Done."
