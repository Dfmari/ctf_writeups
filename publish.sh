#!/bin/bash

echo "[*] Initializing deployment pipeline..."

# 1. Universal Asset Fix
echo "[*] Patching media links in exported HTML..."
find writeups -name "*.html" -exec sed -i '/href="\.\.\/\.\.\/\.\.\/media\// s/class="internal-link is-unresolved"/class="external-link"/g' {} +
find writeups -name "*.html" -exec sed -i '/href="\.\.\/\.\.\/\.\.\/media\// s/class="internal-link"/class="external-link"/g' {} +

# 2. Stage changes
echo "[*] Staging files for commit..."
git add .

# 3. Interactive Commit via Neovim
TMP_MSG=$(mktemp /tmp/git-commit-msg-XXXXXX.txt)
echo "" > "$TMP_MSG"
echo "# Write your commit message above." >> "$TMP_MSG"
echo "# Lines starting with '#' will be ignored." >> "$TMP_MSG"
echo "# If you leave it empty, the commit will abort." >> "$TMP_g"
nvim "$TMP_MSG"

CLEAN_MSG=$(grep -v '^#' "$TMP_MSG" | sed '/^\s*$/d')

if [ -z "$CLEAN_MSG" ]; then
    echo "[!] Commit message empty. Aborting deployment."
    rm "$TMP_MSG"
    exit 1
fi

git commit -F "$TMP_MSG"
rm "$TMP_MSG"


CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)


read -p "[?] Push to which branch? [Press Enter for '$CURRENT_BRANCH']: " TARGET_BRANCH


if [ -z "$TARGET_BRANCH" ]; then
    TARGET_BRANCH=$CURRENT_BRANCH
fi

# 4. Push to the selected branch on GitHub
echo "[*] Pushing to GitHub ($TARGET_BRANCH)..."
git push origin "$TARGET_BRANCH"

echo "[*]  Deployment to '$TARGET_BRANCH' complete!"
