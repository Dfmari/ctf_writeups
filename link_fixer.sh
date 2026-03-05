find writeups -name "*.html" -exec sed -i 's|data-href="\([^"]*\)" href="\.html"|href="\1" download|g' {} +

find writeups -name "*.html" -exec sed -i 's|class="internal-link is-unresolved"|class="external-link"|g' {} +
