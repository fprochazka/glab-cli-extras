cat <<'EOF' | xargs -I{} glab api "projects/group%2Fproject/jobs/{}/trace"
2000
EOF
